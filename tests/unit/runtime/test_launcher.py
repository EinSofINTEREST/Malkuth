"""Unit tests for agent launch wiring.

핵심 계약: **주입한 토큰과 호출에 싣는 토큰이 같다.** 어긋나면 컨테이너는
떴는데 모든 호출이 401 이 된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.core.manifest import AgentManifest
from malkuth.runtime.docker.engine import DockerEngine
from malkuth.runtime.launcher import AgentLauncher
from malkuth.runtime.tokens import AGENT_TOKEN_ENV
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )


def launcher(client: FakeDockerClient | None = None) -> AgentLauncher:
    return AgentLauncher(engine=DockerEngine(client=client or FakeDockerClient()))


async def test_injected_and_carried_tokens_match():
    """발급처와 사용처가 어긋나면 컨테이너는 떴는데 호출이 전부 막힌다."""
    client = FakeDockerClient()
    launched = await launcher(client).start(manifest())

    injected = client.created[0]["environment"][AGENT_TOKEN_ENV]

    assert injected == launched.client._token


async def test_declared_secrets_survive_launch():
    client = FakeDockerClient()

    await launcher(client).start(manifest(), secrets={"ANTHROPIC_API_KEY": "sk-x"})

    assert client.created[0]["environment"]["ANTHROPIC_API_KEY"] == "sk-x"


async def test_client_points_at_the_mapped_port():
    """runtime 이 할당한 포트로 붙어야 한다 — 하드코딩하면 replica 가 깨진다."""
    client = FakeDockerClient(host_port=51000)

    launched = await launcher(client).start(manifest())

    assert "51000" in launched.client._base_url


async def test_each_agent_gets_its_own_token():
    """한 에이전트의 토큰으로 다른 에이전트를 열 수 없어야 한다."""
    launch = launcher()
    first = await launch.start(manifest())
    second = await launch.start(
        manifest().model_copy(
            update={"metadata": manifest().metadata.model_copy(update={"name": "other"})}
        )
    )

    assert first.client._token != second.client._token


async def test_stop_forgets_the_token():
    """죽은 에이전트의 토큰을 들고 있지 않는다."""
    launch = launcher()
    await launch.start(manifest())

    await launch.stop("echo")

    assert launch.issuer.known("echo") is None
    assert launch.launched == {}


async def test_stop_unknown_agent_is_a_noop():
    launch = launcher()

    await launch.stop("never-started")

    assert launch.launched == {}


async def test_stop_all_clears_every_agent():
    launch = launcher()
    await launch.start(manifest())

    await launch.stop_all()

    assert launch.launched == {}


async def test_clients_mapping_feeds_the_node_runtime():
    """ControlNodeRuntime 이 이 매핑을 그대로 받는다."""
    launch = launcher()
    await launch.start(manifest())

    clients = launch.clients()

    assert set(clients) == {"echo"}


async def test_stopped_container_is_removed():
    client = FakeDockerClient()
    launch = launcher(client)
    launched = await launch.start(manifest())

    await launch.stop("echo")

    assert launched.handle.container_id in client.removed


@pytest.mark.parametrize("replica", [0, 2])
async def test_replica_index_reaches_the_container_name(replica):
    client = FakeDockerClient()

    await launcher(client).start(manifest(), replica=replica)

    assert client.created[0]["name"].endswith(str(replica))
