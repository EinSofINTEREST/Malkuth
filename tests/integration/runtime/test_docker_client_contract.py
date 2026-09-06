"""Every DockerClient implementation must behave the same.

프로덕션 구현(`SdkDockerClient`)과 테스트 참조 구현(`CliDockerClient`)이 갈리면
E2E 는 통과하고 실제 배포는 다르게 동작한다 (#243). 같은 계약 테스트를 둘 다 통과한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from malkuth.runtime.docker.client import SdkDockerClient
from malkuth.runtime.docker.engine import DockerClient
from tests.integration.runtime.test_docker_lifecycle import (
    CliDockerClient,
    docker,
    echo_image,  # noqa: F401 — fixture
    requires_docker,
)

pytestmark = [pytest.mark.integration, requires_docker]
NETWORK = "malkuth-contract-net"


def spec_kwargs(image: str, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "image": image,
        "environment": {"MALKUTH_AGENT_TOKEN": "contract"},
        "network": NETWORK,
        "ports": {"8080/tcp": ("127.0.0.1", None)},
        "nano_cpus": 500_000_000,
        "mem_limit": 268_435_456,
        "pids_limit": 256,
        "user": "1000:1000",
        "read_only": True,
        "cap_drop": ["ALL"],
        "tmpfs": {"/tmp": "size=64m", "/workspace": "size=64m"},  # noqa: S108 — 컨테이너 안 경로다
        "volumes": {},
        "labels": {"malkuth.contract": "1"},
        "network_mode": None,
        "publish_all_ports": False,
    }


@pytest.fixture(params=[CliDockerClient, SdkDockerClient], ids=["cli", "sdk"])
def client(request) -> DockerClient:
    return request.param()


@pytest.fixture
def container(client: DockerClient, echo_image: str):  # noqa: F811
    """만들고 반드시 지운다 — 유령 컨테이너는 05 Consistency 3 위반이다."""
    client.ensure_image(echo_image)
    client.ensure_network(NETWORK)
    name = f"contract-{type(client).__name__.lower()}"
    docker("rm", "-f", name, check=False)
    container_id = client.create(**spec_kwargs(echo_image, name))
    try:
        yield container_id
    finally:
        client.remove(container_id)


def test_the_protocol_is_satisfied(client):
    assert isinstance(client, DockerClient)


def test_a_missing_image_is_an_error_not_a_silent_pass(client):
    with pytest.raises(Exception):  # noqa: B017 — 종류는 엔진이 RT_004 로 옮긴다
        client.ensure_image("malkuth/does-not-exist:0.0.0")


def test_create_start_inspect_port_stop_round_trip(client, container):
    client.start(container)

    state = client.inspect(container)
    assert state["Running"] is True
    assert client.port_of(container, 8080) > 0

    client.stop(container, timeout_s=5)

    assert client.inspect(container)["Running"] is False


def test_the_security_settings_reach_the_container(client, container):
    """02 Docker Isolation — 표기가 아니라 컨테이너에 걸린 값을 본다."""
    raw = docker(
        "inspect",
        "--format",
        " ".join(
            f"{{{{.HostConfig.{field}}}}}"
            for field in ("ReadonlyRootfs", "PidsLimit", "NanoCpus", "Memory", "CapDrop")
        ),
        container,
    )

    assert raw.split() == ["true", "256", "500000000", "268435456", "[ALL]"]


def test_find_resolves_a_name_to_the_live_id_only(client, container):
    """재부착은 이름으로 찾는다 — 재시작마다 id 가 바뀌기 때문이다."""
    container_id = container
    name = f"contract-{type(client).__name__.lower()}"
    assert client.find(name) == container_id
    assert client.find("malkuth-no-such-container") is None
    client.remove(container_id)
    assert client.find(name) is None


def test_remove_is_idempotent(client, container):
    client.remove(container)

    client.remove(container)  # 두 번째도 조용히 성공해야 정리 경로가 안전하다
