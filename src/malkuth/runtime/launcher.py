"""Agent launch — spec, token, and client in one place.

에이전트 기동의 조립 지점. 컨테이너 스펙 생성 · 토큰 발급 · Control API
클라이언트 구성을 **한 곳에서** 수행한다.

흩어져 있으면 주입한 토큰과 호출에 싣는 토큰이 어긋나 컨테이너는 떴는데
모든 호출이 401 이 되는 상태가 만들어진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from malkuth.runtime.control import ControlClient
from malkuth.runtime.spec import build_container_spec
from malkuth.runtime.tokens import TokenIssuer, authenticated_env

if TYPE_CHECKING:
    from collections.abc import Mapping

    from malkuth.core.manifest import AgentManifest
    from malkuth.runtime.docker.engine import ContainerHandle, DockerEngine

log = structlog.get_logger(__name__)


@dataclass
class LaunchedAgent:
    """A started agent and the client that can reach it.

    기동된 에이전트와 그것에 닿을 수 있는 클라이언트. 토큰은 이미 양쪽에
    반영되어 있다.
    """

    agent: str
    handle: ContainerHandle
    client: ControlClient

    async def aclose(self) -> None:
        """클라이언트 연결을 정리한다."""
        await self.client.aclose()


@dataclass
class AgentLauncher:
    """Starts agents with authentication wired end to end.

    인증이 양쪽에 배선된 채로 에이전트를 기동한다.

    Attributes:
        engine: Creates and starts the container.
        issuer: Mints the token injected into the container and presented by
            the client — **같은 발급자**여야 둘이 일치한다.
    """

    engine: DockerEngine
    issuer: TokenIssuer = field(default_factory=TokenIssuer)
    launched: dict[str, LaunchedAgent] = field(default_factory=dict)

    async def start(
        self,
        manifest: AgentManifest,
        *,
        secrets: Mapping[str, str] | None = None,
        replica: int = 0,
        a2a_port: int | None = None,
    ) -> LaunchedAgent:
        """Start one agent with its token injected and wired.

        토큰을 주입하고 클라이언트에 물린 채로 에이전트를 기동합니다.

        Args:
            manifest: The validated agent manifest.
            secrets: Secret values resolved from the scope chain.
            replica: Replica index for the container name.
            a2a_port: A2A port assigned by the runtime, when enabled.

        Returns:
            The launched agent — its client already carries the token.
        """
        agent = manifest.name
        spec = build_container_spec(
            manifest,
            env=authenticated_env(self.issuer, agent, secrets),
            replica=replica,
            a2a_port=a2a_port,
            network=self.engine.network,
        )

        handle = await self.engine.start(spec)
        client = ControlClient(
            f"http://127.0.0.1:{handle.control_port}",
            agent=agent,
            # 컨테이너에 주입한 것과 같은 토큰 — 발급자가 하나이므로 일치한다
            token=self.issuer.issue(agent),
        )

        launched = LaunchedAgent(agent=agent, handle=handle, client=client)
        self.launched[agent] = launched
        log.info(
            "agent launched",
            agent=agent,
            container_id=handle.short_id,
            image=handle.image,
            port=handle.control_port,
        )
        return launched

    async def stop(self, agent: str) -> None:
        """Stop an agent and forget its token.

        에이전트를 정지하고 토큰을 버립니다 — 죽은 토큰을 들고 있지 않습니다.
        """
        launched = self.launched.pop(agent, None)
        if launched is None:
            return

        await launched.aclose()
        await self.engine.stop(launched.handle)
        self.issuer.forget(agent)

    async def stop_all(self) -> None:
        """기동된 에이전트를 전부 정지한다 — 하나가 실패해도 나머지를 계속 정리한다."""
        for agent in list(self.launched):
            await self.stop(agent)

    def clients(self) -> dict[str, ControlClient]:
        """노드 런타임이 쓰는 에이전트별 클라이언트 매핑."""
        return {name: item.client for name, item in self.launched.items()}


__all__ = ["AgentLauncher", "LaunchedAgent"]
