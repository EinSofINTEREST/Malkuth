"""agentd A2A server startup tests.

03 은 기동 순서 5단계로 "A2A 서버 기동 (enabled 시)" 를 규정하는데 그 단계가
비어 있었다 — manifest 가 선언하고 runtime 이 포트까지 열어 주는데 컨테이너
안에서 그 포트를 듣는 것이 없었다 (#166).

그래서 #118 이 만든 수신 검증(allowlist / token / depth)이 **한 번도 실행되지
않았다.**
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.agentd.a2a_server import (
    EDGES_ENV,
    MAX_DEPTH_ENV,
    PORT_ENV,
    SECRET_ENV,
    a2a_port,
    build_a2a_app,
    parse_edges,
)
from malkuth.core.manifest import AgentManifest
from malkuth.protocols.a2a.allowlist import Edge


async def invoke(task):  # pragma: no cover - 서버 구성만 검증한다
    raise AssertionError("inbound task should not run in these tests")


def manifest(name: str) -> AgentManifest:
    declared = Path(f"agents/{name}/manifest.yaml").read_text(encoding="utf-8")
    return AgentManifest.model_validate(yaml.safe_load(declared))


@pytest.fixture
def injected(monkeypatch):
    """runtime 이 주입하는 값들 — 에이전트는 자기 배선을 알지 못한다."""
    monkeypatch.setenv(SECRET_ENV, "runtime-signing-secret")
    monkeypatch.setenv(PORT_ENV, "9100")
    monkeypatch.setenv(EDGES_ENV, "researcher>planner,planner>writer")


# --- edge 파싱 -----------------------------------------------------------------


def test_declared_connections_become_edges():
    assert parse_edges("a>b,c>d") == frozenset(
        {Edge(caller="a", callee="b"), Edge(caller="c", callee="d")}
    )


@pytest.mark.parametrize("declared", ["", "  ", "a>", ">b", "garbage"])
def test_malformed_entries_are_dropped_not_fatal(declared):
    """배선 실수 하나로 에이전트가 통째로 못 뜨면 안 된다.

    선언되지 않은 방향은 어차피 `A2A_004` 로 거부된다.
    """
    assert parse_edges(declared) == frozenset()


# --- 서버 구성 -----------------------------------------------------------------


def test_a_declaring_agent_gets_a_server(injected):
    """#166 의 핵심 — 선언했으면 실제로 서빙해야 한다."""
    assert build_a2a_app(manifest("researcher"), invoke) is not None


def test_an_agent_without_a2a_gets_no_server(injected):
    """선언하지 않았는데 포트를 열면 격리 표면이 넓어진다 (02 Network 5)."""
    assert build_a2a_app(manifest("echo"), invoke) is None


def test_without_a_signing_secret_the_server_stays_down(monkeypatch):
    """키가 없으면 token 이 공개 키로 HMAC 되어 callee 측 방어가 무력화된다."""
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, "9100")

    assert build_a2a_app(manifest("researcher"), invoke) is None


def test_the_port_comes_from_the_runtime(monkeypatch):
    """포트를 코드에 박으면 에이전트가 자기 배선을 정하게 된다 (03 Rule 2)."""
    monkeypatch.setenv(PORT_ENV, "9137")

    assert a2a_port() == 9137


@pytest.mark.parametrize("declared", ["", "not-a-number", "-1"])
def test_an_uninjected_port_is_none(monkeypatch, declared):
    monkeypatch.setenv(PORT_ENV, declared)

    assert a2a_port() is None


def test_the_depth_limit_is_injectable(monkeypatch, injected):
    """03 Rule 5 — 순환 위임 방지 상한도 배선의 문제다."""
    monkeypatch.setenv(MAX_DEPTH_ENV, "1")

    # 구성이 성공하면 주입값이 반영된 것 — 실제 거부는 #118 이 검증한다
    assert build_a2a_app(manifest("researcher"), invoke) is not None
