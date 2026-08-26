"""Integration tests for the Docker runtime lifecycle.

실제 컨테이너로 start → health ready → invoke → stop 전 주기를 검증한다.
Docker 가 없으면 skip 한다 (게이트를 막지 않는다).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from malkuth.core.agent import HealthState, HealthStatus
from malkuth.core.errors import MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.runtime.docker.engine import ContainerHandle, DockerEngine
from malkuth.runtime.health import HealthMonitor
from malkuth.runtime.lifecycle import AgentLifecycle, AgentState
from malkuth.runtime.spec import build_container_spec

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_DIR = REPO_ROOT / "deployments" / "docker"
ECHO_MANIFEST = REPO_ROOT / "agents" / "echo" / "manifest.yaml"
TEST_NETWORK = "malkuth-net-lifecycle"
DOCKER_BIN = shutil.which("docker")


def docker(*args: str, check: bool = True) -> str:
    """docker CLI 호출."""
    result = subprocess.run(  # noqa: S603
        [DOCKER_BIN or "docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(  # noqa: S603
            [DOCKER_BIN or "docker", "info"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


requires_docker = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")


class CliDockerClient:
    """docker CLI 를 쓰는 DockerClient 구현.

    SDK 대신 CLI 를 쓰는 이유는 테스트가 SDK 버전에 묶이지 않게 하기 위해서다 —
    검증 대상은 우리 계층의 순서와 정리이지 SDK 바인딩이 아니다.
    """

    def __init__(self) -> None:
        self.containers: list[str] = []

    def ensure_image(self, image: str) -> None:
        if not docker("images", "-q", image, check=False):
            raise RuntimeError(f"image not built: {image}")

    def ensure_network(self, name: str) -> None:
        docker("network", "create", "--driver", "bridge", name, check=False)

    def create(self, **kwargs: Any) -> str:
        args = [
            "create",
            "--name",
            kwargs["name"],
            "--network",
            kwargs["network"],
            "--read-only",
            "--user",
            kwargs["user"],
            f"--pids-limit={kwargs['pids_limit']}",
            "--memory",
            str(kwargs["mem_limit"]),
            "-P",
        ]
        for capability in kwargs.get("cap_drop", ()):
            args.append(f"--cap-drop={capability}")
        for path in kwargs.get("tmpfs", {}):
            args.append(f"--tmpfs={path}")
        for key, value in kwargs.get("environment", {}).items():
            args.extend(["-e", f"{key}={value}"])
        args.append(kwargs["image"])

        container_id = docker(*args)
        self.containers.append(container_id)
        return container_id

    def start(self, container_id: str) -> None:
        docker("start", container_id)

    def inspect(self, container_id: str) -> dict[str, Any]:
        raw = docker("inspect", "--format", "{{json .State}}", container_id)
        state: dict[str, Any] = json.loads(raw)
        return state

    def port_of(self, container_id: str, container_port: int) -> int:
        mapping = docker("port", container_id, f"{container_port}/tcp")
        return int(mapping.splitlines()[0].rsplit(":", 1)[-1])

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        docker("stop", "-t", str(int(timeout_s)), container_id)

    def remove(self, container_id: str) -> None:
        docker("rm", "-f", container_id, check=False)


def fetch(url: str, body: dict | None = None, *, timeout: float = 10.0) -> dict:
    """루프백 Control API 호출 — blocking 이므로 async 경로에서는 to_thread 로 감싼다."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        url, data=data, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        result: dict = json.loads(response.read())
        return result


class HttpProbe:
    """Control API 를 직접 호출하는 probe."""

    def __init__(self, port: int) -> None:
        self._port = port

    async def health(self) -> HealthStatus:
        payload = await asyncio.to_thread(
            fetch, f"http://127.0.0.1:{self._port}/v1/health", timeout=5.0
        )
        return HealthStatus.model_validate(payload)


@pytest.fixture(scope="module")
def echo_image() -> str:
    """echo 이미지 — base 와 함께 빌드한다."""
    docker(
        "build",
        "-t",
        "malkuth/agent-base:0.1.0",
        "-f",
        str(DOCKER_DIR / "agent-base.Dockerfile"),
        str(REPO_ROOT),
    )
    docker(
        "build",
        "-t",
        "malkuth/agent-echo:0.1.0",
        "--build-arg",
        "BASE_TAG=0.1.0",
        "-f",
        str(DOCKER_DIR / "agent-echo.Dockerfile"),
        str(REPO_ROOT),
    )
    return "malkuth/agent-echo:0.1.0"


@pytest.fixture
def engine(echo_image: str) -> Iterator[tuple[DockerEngine, CliDockerClient]]:
    """엔진 — finalizer 가 컨테이너와 네트워크를 반드시 정리한다."""
    client = CliDockerClient()
    engine = DockerEngine(client=client, network=TEST_NETWORK, stop_grace_s=5.0)
    try:
        yield engine, client
    finally:
        for container_id in client.containers:
            docker("rm", "-f", container_id, check=False)
        docker("network", "rm", TEST_NETWORK, check=False)


def echo_spec(**overrides: Any):
    """echo 에이전트의 컨테이너 스펙."""
    import yaml

    manifest = AgentManifest.model_validate(
        yaml.safe_load(ECHO_MANIFEST.read_text(encoding="utf-8"))
    )
    return build_container_spec(
        manifest,
        env={"MALKUTH_EXECUTOR": "echo"},
        network=TEST_NETWORK,
        base_image="malkuth/agent-echo:0.1.0",
        **overrides,
    )


async def wait_ready(monitor: HealthMonitor, *, attempts: int = 60) -> AgentState:
    """Ready 로 전환될 때까지 확인한다."""
    state = AgentState.STARTING
    for _ in range(attempts):
        state = await monitor.check_once()
        if monitor.consecutive_failures == 0:
            return state
        await asyncio.sleep(1)
    return state


def ready_lifecycle() -> AgentLifecycle:
    lifecycle = AgentLifecycle(agent="echo")
    lifecycle.transition(AgentState.BUILT)
    lifecycle.transition(AgentState.STARTING)
    lifecycle.transition(AgentState.READY)
    return lifecycle


@requires_docker
async def test_full_lifecycle_start_health_invoke_stop(engine):
    """start → health ready → invoke → stop 전 주기."""
    docker_engine, client = engine

    handle = await docker_engine.start(echo_spec())
    assert isinstance(handle, ContainerHandle)

    monitor = HealthMonitor(
        agent="echo",
        probe=HttpProbe(handle.control_port),
        lifecycle=ready_lifecycle(),
        timeout_s=5.0,
    )
    assert await wait_ready(monitor) is AgentState.READY

    result = await asyncio.to_thread(
        fetch,
        f"http://127.0.0.1:{handle.control_port}/v1/invoke",
        {
            "task_id": "t-1",
            "run_id": "direct-1",
            "node_id": None,
            "input": {"msg": "hi"},
            "trace": {"trace_id": "tr-1"},
        },
    )
    assert result["output"] == {"msg": "hi"}

    await docker_engine.stop(handle)

    with pytest.raises((urllib.error.URLError, OSError)):
        await asyncio.to_thread(
            fetch, f"http://127.0.0.1:{handle.control_port}/v1/health", timeout=3.0
        )


@requires_docker
async def test_missing_image_is_rt_004(engine):
    """존재하지 않는 이미지는 기동 전에 걸러진다."""
    docker_engine, _ = engine

    with pytest.raises(MalkuthError) as exc_info:
        await docker_engine.start(echo_spec(base_image="malkuth/absent:9.9.9"))

    assert exc_info.value.code == "RT_004"


@requires_docker
async def test_stop_removes_the_container(engine):
    """정리 후 잔여물이 없어야 한다."""
    docker_engine, _ = engine
    handle = await docker_engine.start(echo_spec())

    await docker_engine.stop(handle)

    assert docker("ps", "-aq", "--filter", f"id={handle.container_id}", check=False) == ""


@requires_docker
async def test_health_reports_healthy_through_the_monitor(engine):
    """HealthMonitor 가 실제 Control API 로부터 healthy 를 읽는다."""
    docker_engine, _ = engine
    handle = await docker_engine.start(echo_spec())
    monitor = HealthMonitor(
        agent="echo",
        probe=HttpProbe(handle.control_port),
        lifecycle=ready_lifecycle(),
        timeout_s=5.0,
    )

    await wait_ready(monitor)

    assert monitor.last_status is HealthState.HEALTHY
