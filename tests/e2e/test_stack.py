"""End-to-end tests over the full compose stack.

compose 로 전체 스택을 띄우고 실제 경로를 검증한다. 모델은 결정적 fake
provider — E2E 에서도 실 LLM 을 호출하지 않는다 (06 Testing 3).

Docker 가 없으면 skip 한다. nightly CI 에서만 실행된다 (PR gate 아님).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deployments" / "docker" / "compose.e2e.yaml"
ECHO_URL = "http://127.0.0.1:18081"
READY_TIMEOUT_S = 120.0
DOCKER_BIN = shutil.which("docker")


def docker(*args: str, check: bool = True, timeout: int = 600) -> str:
    """docker CLI 호출."""
    result = subprocess.run(  # noqa: S603
        [DOCKER_BIN or "docker", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def docker_available() -> bool:
    if DOCKER_BIN is None:
        return False
    return (
        subprocess.run(  # noqa: S603
            [DOCKER_BIN, "info"], capture_output=True, check=False
        ).returncode
        == 0
    )


requires_docker = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")


def fetch(url: str, body: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict:
    """Control API 호출."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        url, data=data, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        result: dict = json.loads(response.read())
        return result


def wait_healthy(url: str, *, timeout: float = READY_TIMEOUT_S) -> bool:
    """Control API 가 healthy 를 보고할 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if fetch(f"{url}/v1/health", timeout=3.0)["status"] == "healthy":
                return True
        except (urllib.error.URLError, OSError, KeyError):
            pass
        time.sleep(2)
    return False


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    """전체 스택 — finalizer 가 컨테이너와 볼륨을 반드시 정리한다."""
    docker("compose", "-f", str(COMPOSE_FILE), "up", "-d", "--build")
    try:
        yield
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


def direct_task(payload: dict[str, Any], task_id: str = "e2e-1") -> dict[str, Any]:
    """직렬화된 direct TaskRequest — trace 는 필수다."""
    return {
        "task_id": task_id,
        "run_id": "direct-e2e",
        "node_id": None,
        "input": payload,
        "trace": {"trace_id": "trace-e2e"},
    }


@requires_docker
def test_stack_becomes_healthy(stack):
    """compose 로 띄운 스택이 healthy 로 전환된다."""
    assert wait_healthy(ECHO_URL)


@requires_docker
def test_fake_provider_is_healthy(stack):
    """모델은 결정적 fake — 실 LLM 을 호출하지 않는다."""
    assert wait_healthy(ECHO_URL)

    state = docker("compose", "-f", str(COMPOSE_FILE), "ps", "--format", "json", "fake-provider")

    assert state


@requires_docker
def test_direct_request_reaches_a_running_agent(stack):
    """상주 스택에 붙은 에이전트가 그래프 run 과 무관한 직접 호출에 응답한다."""
    assert wait_healthy(ECHO_URL)

    result = fetch(f"{ECHO_URL}/v1/invoke", direct_task({"msg": "hello"}))

    assert result["status"] == "completed"
    assert result["output"] == {"msg": "hello"}


@requires_docker
def test_direct_request_does_not_need_a_graph_run(stack):
    """direct 태스크는 node_id 가 없고 어떤 run 의 state 도 건드리지 않는다."""
    assert wait_healthy(ECHO_URL)

    result = fetch(f"{ECHO_URL}/v1/invoke", direct_task({"a": 1}, task_id="e2e-direct"))

    assert result["task_id"] == "e2e-direct"


@requires_docker
def test_streaming_emits_events(stack):
    """스트리밍 경로가 SSE 로 이벤트를 흘린다."""
    assert wait_healthy(ECHO_URL)

    request = urllib.request.Request(  # noqa: S310
        f"{ECHO_URL}/v1/stream",
        data=json.dumps(direct_task({"msg": "stream"})).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        body = response.read().decode()

    assert body.count("data: ") >= 2
    assert '"type":"done"' in body.replace(" ", "")


@requires_docker
def test_untraceable_task_is_rejected(stack):
    """모든 태스크는 단일 trace 로 묶여야 한다."""
    assert wait_healthy(ECHO_URL)
    payload = direct_task({"msg": "x"})
    del payload["trace"]

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fetch(f"{ECHO_URL}/v1/invoke", payload)

    assert exc_info.value.code == 422


@requires_docker
def test_agent_runs_as_non_root_in_the_stack(stack):
    """격리 계약은 compose 스택에서도 유지된다."""
    assert wait_healthy(ECHO_URL)

    uid = docker("compose", "-f", str(COMPOSE_FILE), "exec", "-T", "agent-echo", "id", "-u")

    assert uid == "1000"


@requires_docker
def test_card_is_served(stack):
    """A2A AgentCard 가 Control API 로 제공된다."""
    assert wait_healthy(ECHO_URL)

    card = fetch(f"{ECHO_URL}/v1/card")

    assert card["name"] == "echo"
