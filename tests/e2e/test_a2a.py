"""A2A across the container boundary.

#118 이 SDK 를 바인딩하고 #166 이 서버를 띄웠지만, 지금까지 그 경로는
**같은 프로세스 안에서만** 검증됐다 (ASGI). 여기서는 실제 컨테이너 사이로
왕복시킨다 (#162).

allowlist 는 그래프의 `connections` 선언이 원본이다 —
`research-pipeline.yaml` 은 `researcher → planner` 만 선언하므로 역방향은
`A2A_004` 로 거부돼야 한다.
"""

from __future__ import annotations

import os

import pytest

from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.protocols.a2a.allowlist import Edge, issue_token
from malkuth.protocols.a2a.sdk import SdkPeerTransport
from tests.e2e.test_graph_run import graph_stack  # noqa: F401 — fixture 재사용
from tests.e2e.test_stack import requires_docker
from tests.fixtures.builders import make_task

pytestmark = pytest.mark.e2e

# compose 가 여는 A2A 포트 — 컨테이너 안에서는 전부 9100 이다
A2A_PORTS = {"planner": 19102, "researcher": 19103, "writer": 19104}

SECRET = os.environ.get("MALKUTH_A2A_SECRET", "e2e-a2a-secret").encode("utf-8")


def transport_for(caller: str) -> SdkPeerTransport:
    """caller 가 보는 peer 주소 — runtime 이 주입하는 것을 여기서는 테스트가 한다."""
    return SdkPeerTransport(
        agent=caller,
        addresses={name: f"http://127.0.0.1:{port}" for name, port in A2A_PORTS.items()},
        timeout_s=30.0,
    )


def token_for(caller: str, callee: str) -> str:
    return issue_token(SECRET, Edge(caller=caller, callee=callee))


@requires_docker
async def test_a_declared_call_crosses_the_container_boundary(graph_stack):  # noqa: F811
    """#162 의 핵심 — 같은 프로세스가 아니라 **컨테이너 사이**를 건넌다."""
    transport = transport_for("researcher")
    try:
        result = await transport.send(
            callee="planner",
            task=make_task(node_id="planner", input={"query": "무엇부터 볼까"}),
            token=token_for("researcher", "planner"),
            headers={},
        )
    finally:
        await transport.close()

    assert result.output


@requires_docker
async def test_an_undeclared_direction_is_refused(graph_stack):  # noqa: F811
    """그래프는 researcher→planner 만 선언한다 — 역방향은 거부돼야 한다.

    03 Rule 6: 호출 방향은 순전히 선언의 문제다.
    """
    transport = transport_for("planner")
    try:
        with pytest.raises(MalkuthError) as exc_info:
            await transport.send(
                callee="researcher",
                task=make_task(node_id="researcher"),
                token=token_for("planner", "researcher"),
                headers={},
            )
    finally:
        await transport.close()

    assert exc_info.value.code == ErrorCode.A2A_004


@requires_docker
async def test_a_forged_token_is_refused(graph_stack):  # noqa: F811
    """이름만 주장하는 호출을 막는 것이 per-edge token 의 존재 이유다."""
    transport = transport_for("researcher")
    try:
        with pytest.raises(MalkuthError) as exc_info:
            await transport.send(
                callee="planner",
                task=make_task(node_id="planner"),
                token="forged-token",
                headers={},
            )
    finally:
        await transport.close()

    assert exc_info.value.code == ErrorCode.A2A_004


@requires_docker
async def test_a_deep_chain_is_refused(graph_stack):  # noqa: F811
    """깊이 상한은 수신 측에서도 본다 — caller 의 정직함에 기대면 순환이 뚫린다."""
    from malkuth.core.agent import TraceContext

    transport = transport_for("researcher")
    try:
        with pytest.raises(MalkuthError) as exc_info:
            await transport.send(
                callee="planner",
                task=make_task(node_id="planner", trace=TraceContext(trace_id="deep", depth=9)),
                token=token_for("researcher", "planner"),
                headers={},
            )
    finally:
        await transport.close()

    assert exc_info.value.code == ErrorCode.A2A_005
