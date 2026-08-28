"""A2A server startup inside the agent container.

03 은 기동 순서의 5단계로 "A2A 서버 기동 (enabled 시)" 를 규정하는데 그
단계가 비어 있었다 — manifest 가 선언하고 runtime 이 포트까지 열어 주는데
컨테이너 안에서 그 포트를 듣는 것이 없었다 (#166).

포트·allowlist·서명 키는 **runtime 이 주입**한다. 에이전트는 자기 배선을
알지 못하며 (02 Rule 6), 포트를 코드에 박지 않는다 (03 Rule 2).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog

from malkuth.protocols.a2a.allowlist import Allowlist, Edge

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from malkuth.core.agent import TaskRequest, TaskResult
    from malkuth.core.manifest import AgentManifest

PORT_ENV = "MALKUTH_A2A_PORT"
"""runtime 이 할당한 A2A 포트 — 미주입 시 서버를 띄우지 않는다."""

ADVERTISED_HOST_ENV = "MALKUTH_A2A_ADVERTISED_HOST"
"""카드에 실을 도달 주소.

**바인드 주소(`0.0.0.0`)를 광고하면 안 된다** — peer 가 그리로 접속하려다
실패한다. 미주입 시 에이전트 이름을 쓴다: 전용 bridge 네트워크에서 컨테이너
이름이 곧 DNS 이름이다 (02 Network 5).
"""

EDGES_ENV = "MALKUTH_A2A_EDGES"
"""선언된 연결 — ``caller>callee`` 를 쉼표로. 그래프 config 가 원본이다."""

SECRET_ENV = "MALKUTH_A2A_SECRET"  # noqa: S105 — 키 이름이지 값이 아니다
"""per-edge token 서명 키 — runtime 이 발급한다."""

MAX_DEPTH_ENV = "MALKUTH_A2A_MAX_DEPTH"

PEERS_ENV = "MALKUTH_A2A_PEERS"
"""peer 주소 — ``name=host:port`` 를 쉼표로. **runtime 이 주입한다.**

03 Discovery: 에이전트는 peer 의 주소를 스스로 알아내지 않는다. 이 값이 없으면
받을 수는 있어도 걸 수는 없다 — 그것이 #193 의 상태였다.
"""

log = structlog.get_logger(__name__)


def parse_edges(declared: str) -> frozenset[Edge]:
    """``caller>callee`` 목록을 edge 집합으로.

    빈 항목은 버린다 — 형식 오류로 기동을 막으면 배선 실수 하나가 에이전트를
    통째로 못 뜨게 한다. 선언되지 않은 방향은 어차피 거부된다.
    """
    edges = set()
    for entry in declared.split(","):
        caller, _, callee = entry.strip().partition(">")
        if caller and callee:
            edges.add(Edge(caller=caller, callee=callee))
    return frozenset(edges)


def parse_peers(declared: str) -> dict[str, str]:
    """``name=host:port`` 목록을 주소 매핑으로.

    빈 항목은 버린다 — 배선 실수 하나로 에이전트를 통째로 못 뜨게 하지 않는다.
    선언되지 않은 peer 는 어차피 allowlist 가 거부한다.
    """
    peers: dict[str, str] = {}
    for entry in declared.split(","):
        name, _, address = entry.strip().partition("=")
        if name and address:
            peers[name] = address if "://" in address else f"http://{address}"
    return peers


def build_peer_client(manifest: AgentManifest) -> Any:
    """Assemble the client this agent calls its peers with.

    이 에이전트가 peer 를 부를 때 쓰는 클라이언트를 조립합니다.

    03 은 "실행 중 에이전트가 allowlist 에 선언된 peer 에게 위임/질의한다" 를
    규정하는데, 이것을 조립하는 곳이 없어 **받을 수는 있고 걸 수는 없는**
    상태였다 (#193).

    Args:
        manifest: The agent manifest — 이름이 caller 신원이 된다.

    Returns:
        The client, or None when nothing is wired (주소나 서명 키 부재).
    """
    peers = parse_peers(os.environ.get(PEERS_ENV, ""))
    secret = os.environ.get(SECRET_ENV, "")
    if not peers or not secret:
        # 주소가 없으면 부를 곳이 없고, 서명 키가 없으면 callee 가 거부한다
        return None

    from malkuth.protocols.a2a.client import A2AClient
    from malkuth.protocols.a2a.sdk import SdkPeerTransport

    return A2AClient(
        agent=manifest.name,
        allowlist=Allowlist(
            edges=parse_edges(os.environ.get(EDGES_ENV, "")),
            secret=secret.encode("utf-8"),
            max_depth=int(os.environ.get(MAX_DEPTH_ENV, str(_default_max_depth()))),
        ),
        transport=SdkPeerTransport(agent=manifest.name, addresses=peers),
    )


def _default_max_depth() -> int:
    """server 쪽과 같은 기본값을 쓴다 — 두 방향이 어긋나면 깊이 판정이 갈린다."""
    from malkuth.protocols.a2a.server import DEFAULT_MAX_DEPTH

    return int(DEFAULT_MAX_DEPTH)


def build_a2a_app(
    manifest: AgentManifest,
    invoke: Callable[[TaskRequest], Awaitable[TaskResult]],
) -> Any | None:
    """Build the inbound A2A app when this agent declares it.

    이 에이전트가 A2A 를 선언했을 때 수신 앱을 만듭니다.

    Args:
        manifest: The validated agent manifest.
        invoke: Runs one inbound task — 검증을 통과한 호출만 닿습니다.

    Returns:
        The FastAPI app, or ``None`` when A2A is not enabled or the runtime
        did not inject a port — **포트 없이 임의로 고르면 03 Rule 2 를
        어긴다** (에이전트가 자기 배선을 정하게 된다).
    """
    if not manifest.spec.a2a.enabled:
        return None

    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        # 서명 키가 없으면 token 이 공개된 키로 HMAC 되어 callee 측 방어가
        # 통째로 무력화된다 (allowlist.py 의 같은 이유)
        log.warning("a2a disabled: no signing secret injected", agent=manifest.name)
        return None

    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore
    from fastapi import FastAPI

    from malkuth.protocols.a2a.client import A2AServer
    from malkuth.protocols.a2a.server import DEFAULT_MAX_DEPTH, GuardedExecutor, InboundGuard

    allowlist = Allowlist(
        edges=parse_edges(os.environ.get(EDGES_ENV, "")),
        secret=secret.encode("utf-8"),
    )
    guard = InboundGuard(
        server=A2AServer(agent=manifest.name, allowlist=allowlist),
        max_depth=int(os.environ.get(MAX_DEPTH_ENV, str(DEFAULT_MAX_DEPTH))),
    )

    card = _protobuf_card(manifest)
    handler = DefaultRequestHandler(
        agent_executor=GuardedExecutor(guard, invoke),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )
    log.info("a2a server ready", agent=manifest.name, port=a2a_port())
    return app


def a2a_port() -> int | None:
    """runtime 이 할당한 포트 — 미주입 시 None."""
    declared = os.environ.get(PORT_ENV, "").strip()
    return int(declared) if declared.isdigit() else None


def _protobuf_card(manifest: AgentManifest) -> Any:
    """SDK 가 라우팅에 쓰는 카드 — manifest 에서 만든다 (03: 수동 작성 금지)."""
    from a2a.types import a2a_pb2 as pb
    from a2a.utils.constants import TransportProtocol

    capabilities = manifest.spec.a2a.capabilities
    return pb.AgentCard(
        name=manifest.name,
        description=manifest.metadata.description or manifest.name,
        version=manifest.metadata.version,
        capabilities=pb.AgentCapabilities(
            streaming=bool(getattr(capabilities, "streaming", False))
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            pb.AgentInterface(
                url=f"http://{_advertised_host(manifest)}:{a2a_port() or 0}/",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version="1.0",
            )
        ],
    )


def _advertised_host(manifest: AgentManifest) -> str:
    """peer 가 실제로 닿을 수 있는 호스트."""
    return os.environ.get(ADVERTISED_HOST_ENV, "").strip() or manifest.name


def declared_edges() -> str:
    """현재 선언된 연결 문자열 — 진단용."""
    return os.environ.get(EDGES_ENV, "")


__all__ = [
    "ADVERTISED_HOST_ENV",
    "EDGES_ENV",
    "PEERS_ENV",
    "MAX_DEPTH_ENV",
    "PORT_ENV",
    "SECRET_ENV",
    "a2a_port",
    "build_a2a_app",
    "declared_edges",
    "parse_edges",
]
