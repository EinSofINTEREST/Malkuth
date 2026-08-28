"""Peer delegation from inside a task.

03 은 "실행 중 에이전트가 allowlist 에 선언된 peer 에게 위임/질의한다" 를
규정하는데, `A2AClient` 를 조립하는 곳이 없어 에이전트는 **받을 수는 있고
걸 수는 없었다** (#193).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from malkuth.core.agent import TaskResult, TraceContext
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.core.skill import SkillContext
from malkuth.protocols.a2a.tool import ASK_PEER_TOOL, peer_spec, run_ask_peer

REPO_ROOT = Path(__file__).resolve().parents[4]


class RecordingClient:
    """위임을 기록하는 A2A 클라이언트 대역."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def call(self, callee: str, task: Any) -> TaskResult:
        self.calls.append((callee, task))
        return TaskResult.completed(task, output={"answer": "42"})


def context(*, depth: int = 0) -> SkillContext:
    return SkillContext(
        agent="researcher",
        task_id="task-1",
        run_id="run-1",
        trace=TraceContext(trace_id="trace-1", graph="research-pipeline", depth=depth),
    )


async def test_a_declared_peer_is_reached():
    """#193 — 조립이 없어 이 경로가 존재하지 않았다."""
    client = RecordingClient()

    result = await run_ask_peer(
        client,  # type: ignore[arg-type]
        context(),
        {"peer": "planner", "request": "계획을 다시 봐줘"},
    )

    assert result["peer"] == "planner"
    assert result["output"] == {"answer": "42"}
    assert client.calls[0][0] == "planner"


async def test_the_delegated_task_inherits_the_trace():
    """깊이가 이어지지 않으면 순환 위임이 상한에 걸리지 않는다 (03 Rule 5)."""
    client = RecordingClient()

    await run_ask_peer(
        client,  # type: ignore[arg-type]
        context(depth=2),
        {"peer": "planner", "request": "질문"},
    )

    _, delegated = client.calls[0]
    assert delegated.trace.depth == 2
    assert delegated.trace.trace_id == "trace-1"


async def test_the_delegated_task_keeps_the_run():
    """run 이 갈리면 같은 run 의 메트릭과 메모리가 둘로 나뉜다."""
    client = RecordingClient()

    await run_ask_peer(
        client,  # type: ignore[arg-type]
        context(),
        {"peer": "planner", "request": "질문"},
    )

    assert client.calls[0][1].run_id == "run-1"


@pytest.mark.parametrize(
    "arguments",
    [
        {"request": "질문"},
        {"peer": "", "request": "질문"},
        {"peer": "planner"},
        {"peer": "planner", "request": "  "},
        {"peer": 3, "request": "질문"},
    ],
)
async def test_malformed_arguments_are_refused(arguments):
    """모델이 보내는 인자는 신뢰할 수 없다 — KeyError 로 터지면 원인이 안 보인다."""
    with pytest.raises(MalkuthError) as excinfo:
        await run_ask_peer(RecordingClient(), context(), arguments)  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.VAL_001


def test_the_spec_names_the_reachable_peers():
    """없는 peer 를 지어내면 A2A_004 로 거부되고 한 턴이 낭비된다."""
    described = peer_spec(("planner", "writer")).description

    assert "planner" in described
    assert "writer" in described


def test_the_spec_survives_an_empty_peer_list():
    """연결이 아직 없어도 tool 설명이 깨지면 안 된다."""
    assert peer_spec(()).name == ASK_PEER_TOOL


# --- 배선: 조립을 통과하는 경로 ---------------------------------------------
# tool 만 만들고 아무도 등록하지 않으면 #193 그대로다


def manifest(name: str) -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / name / "manifest.yaml").read_text("utf-8"))
    )


def test_an_a2a_agent_is_offered_the_tool():
    """A2A 를 켠 에이전트는 위임 창구를 봐야 한다."""
    from malkuth.agentd.bootstrap import build_tool_registry

    registry = build_tool_registry((), {}, agent="researcher", with_peers=True)

    assert ASK_PEER_TOOL in registry


def test_an_agent_without_a2a_is_not():
    """부를 수 없는 tool 을 광고하면 모델이 고를 때마다 거부되어 루프에 빠진다."""
    from malkuth.agentd.bootstrap import build_tool_registry

    registry = build_tool_registry((), {}, agent="echo", with_peers=False)

    assert ASK_PEER_TOOL not in registry


def test_the_bootstrap_follows_the_manifest():
    """선언이 곧 배선이다 — a2a.enabled 가 창구를 연다."""
    assert manifest("researcher").spec.a2a.enabled
    assert not manifest("echo").spec.a2a.enabled


async def test_the_registry_routes_the_tool_to_the_client():
    """registry 가 라우팅하지 않으면 등록만 되고 실행이 안 된다."""
    from malkuth.agentd.tools import AgentToolRegistry

    client = RecordingClient()
    registry = AgentToolRegistry(agent="researcher", peers=client)  # type: ignore[arg-type]

    result = await registry.call(ASK_PEER_TOOL, {"peer": "planner", "request": "질문"}, context())

    assert result["peer"] == "planner"


async def test_without_a_client_the_tool_is_unknown():
    """조립되지 않은 창구를 부르면 원인이 드러나야 한다."""
    from malkuth.agentd.tools import AgentToolRegistry

    with pytest.raises(MalkuthError) as excinfo:
        await AgentToolRegistry(agent="researcher").call(ASK_PEER_TOOL, {}, context())

    assert excinfo.value.code == ErrorCode.MOD_001


async def test_the_production_assembly_wires_the_peer_client(monkeypatch):
    """#193 의 핵심 — 조립에서 빠뜨리면 tool 이 있어도 부를 수단이 없다.

    실행기를 **실제 조립 경로로** 만든다. `AgentToolRegistry` 를 직접 만들면
    이 배선 자체를 건너뛴다.
    """
    from malkuth.agentd.__main__ import build_executor, load_manifest
    from malkuth.agentd.a2a_server import EDGES_ENV, PEERS_ENV, SECRET_ENV

    monkeypatch.delenv("MALKUTH_EXECUTOR", raising=False)
    monkeypatch.setenv("MALKUTH_ROOT", str(REPO_ROOT))
    # runtime 이 주입하는 것과 같은 값들 (03 Discovery)
    monkeypatch.setenv(PEERS_ENV, "planner=agent-planner:19102")
    monkeypatch.setenv(EDGES_ENV, "researcher>planner")
    monkeypatch.setenv(SECRET_ENV, "unit-secret")

    built = await build_executor(load_manifest(REPO_ROOT / "agents/researcher/manifest.yaml"))

    assert built._tools.peers is not None


async def test_no_peer_addresses_means_no_client(monkeypatch):
    """주소가 없으면 부를 곳이 없다 — 조용히 빈 클라이언트를 만들면 안 된다."""
    from malkuth.agentd.__main__ import build_executor, load_manifest
    from malkuth.agentd.a2a_server import EDGES_ENV, PEERS_ENV, SECRET_ENV

    monkeypatch.delenv("MALKUTH_EXECUTOR", raising=False)
    monkeypatch.setenv("MALKUTH_ROOT", str(REPO_ROOT))
    for name in (PEERS_ENV, EDGES_ENV, SECRET_ENV):
        monkeypatch.delenv(name, raising=False)

    built = await build_executor(load_manifest(REPO_ROOT / "agents/researcher/manifest.yaml"))

    assert built._tools.peers is None


async def test_a_signing_secret_alone_is_not_enough(monkeypatch):
    """서명 키가 있어도 **주소가 없으면 부를 곳이 없다.**

    빈 클라이언트를 만들면 tool 이 광고되고, 모델이 고를 때마다 실패한다.
    """
    from malkuth.agentd.__main__ import build_executor, load_manifest
    from malkuth.agentd.a2a_server import EDGES_ENV, PEERS_ENV, SECRET_ENV

    monkeypatch.delenv("MALKUTH_EXECUTOR", raising=False)
    monkeypatch.setenv("MALKUTH_ROOT", str(REPO_ROOT))
    monkeypatch.delenv(PEERS_ENV, raising=False)
    monkeypatch.setenv(EDGES_ENV, "researcher>planner")
    monkeypatch.setenv(SECRET_ENV, "unit-secret")

    built = await build_executor(load_manifest(REPO_ROOT / "agents/researcher/manifest.yaml"))

    assert built._tools.peers is None


def test_peer_addresses_are_parsed_from_the_runtime_injection():
    """03 Discovery — 에이전트가 주소를 스스로 알아내지 않는다."""
    from malkuth.agentd.a2a_server import parse_peers

    parsed = parse_peers("planner=agent-planner:19102, writer=http://agent-writer:19104, bad")

    assert parsed == {
        "planner": "http://agent-planner:19102",
        "writer": "http://agent-writer:19104",
    }
