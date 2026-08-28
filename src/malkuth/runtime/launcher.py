"""Agent launch — spec, token, and client in one place.

에이전트 기동의 조립 지점. 컨테이너 스펙 생성 · 토큰 발급 · Control API
클라이언트 구성을 **한 곳에서** 수행한다.

흩어져 있으면 주입한 토큰과 호출에 싣는 토큰이 어긋나 컨테이너는 떴는데
모든 호출이 401 이 되는 상태가 만들어진다.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from malkuth.core.errors import NETWORK_RETRY, ErrorCategory, ErrorCode, MalkuthError
from malkuth.memory.http import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
from malkuth.runtime.control import ControlClient
from malkuth.runtime.lifecycle import AgentLifecycle, AgentState
from malkuth.runtime.ports import A2APortAllocator
from malkuth.runtime.spec import build_container_spec
from malkuth.runtime.tokens import TokenIssuer, authenticated_env

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from malkuth.core.manifest import AgentManifest
    from malkuth.observability.metrics import Metrics
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
    lifecycle: AgentLifecycle = field(default_factory=lambda: AgentLifecycle(agent=""))
    """이 레플리카의 02 lifecycle 상태 — 기동·health·정지가 여기로 모인다."""
    # 포트 회수는 **어느 레플리카였는지** 알아야 한다 — 기동 시점의 값을
    # 들고 있지 않으면 정지가 엉뚱한 레플리카의 포트를 놓아준다
    replica: int = 0

    async def aclose(self) -> None:
        """클라이언트 연결을 정리한다."""
        await self.client.aclose()


@dataclass(frozen=True)
class MemoryEndpoint:
    """Where an agent reaches the Memory Service, and with what token.

    저장소 자격증명이 아니라 **주소와 불투명 토큰**만 담는다 — 그것이 컨테이너에
    들어가도 되는 전부다.
    """

    url: str
    token: str


def _with_memory(env: dict[str, str], memory: MemoryEndpoint | None) -> dict[str, str]:
    """메모리 접속 정보를 환경에 합친다 — 없으면 그대로 둔다."""
    if memory is None:
        return env
    return {**env, MEMORY_URL_ENV: memory.url, MEMORY_TOKEN_ENV: memory.token}


@dataclass
class AgentLauncher:
    """Starts agents with authentication wired end to end.

    인증이 양쪽에 배선된 채로 에이전트를 기동한다.

    Attributes:
        engine: Creates and starts the container.
        issuer: Mints the token injected into the container and presented by
            the client — **같은 발급자**여야 둘이 일치한다.
        ports: Allocates the A2A port from the runtime's range (03 Rule 2).
            None 이면 포트를 배정하지 않는다 — A2A 를 쓰지 않는 배포와,
            포트를 밖에서 정해 넘기는 테스트가 그 경우다.
    """

    engine: DockerEngine
    issuer: TokenIssuer = field(default_factory=TokenIssuer)
    ports: A2APortAllocator | None = None
    metrics: Metrics | None = None
    """`malkuth_agent_health` 를 채울 registry — 미주입 시 계측하지 않는다."""
    health_interval_s: float | None = None
    """health 폴링 주기. **None 이면 감시하지 않는다** — 루프의 소유자가
    분명해야 하므로 감시를 원하는 조립만 켠다 (02 Lifecycle Rules 3)."""
    health_sleep: Callable[[float], object] | None = None
    """06 은 시간 의존 로직이 테스트에서 실제로 자는 것을 금지한다."""
    launched: dict[tuple[str, int], LaunchedAgent] = field(default_factory=dict)
    _cursors: dict[str, int] = field(default_factory=dict, init=False)
    _monitors: dict[tuple[str, int], asyncio.Task[None]] = field(default_factory=dict, init=False)
    """기동된 레플리카 — 키는 **(에이전트, replica)** 다.

    이름만으로 잡으면 두 번째 레플리카가 첫 핸들을 덮어 컨테이너를 미아로
    만든다. 01 Scalability 는 동일 manifest 의 N replica 기동을 규정한다.
    """

    async def start(
        self,
        manifest: AgentManifest,
        *,
        secrets: Mapping[str, str] | None = None,
        replica: int = 0,
        a2a_port: int | None = None,
        memory: MemoryEndpoint | None = None,
    ) -> LaunchedAgent:
        """Start one agent with its token injected and wired.

        토큰을 주입하고 클라이언트에 물린 채로 에이전트를 기동합니다.

        Args:
            manifest: The validated agent manifest.
            secrets: Secret values resolved from the scope chain.
            replica: Replica index for the container name.
            memory: Memory Service address and this agent's opaque token —
                **DB 자격증명은 컨테이너에 넣지 않는다** (09 Access Enforcement 1).
            a2a_port: A2A port to use. 생략하면 할당기가 범위에서 고른다 —
                03 은 포트를 runtime 이 준다고 규정한다.

        Returns:
            The launched agent — its client already carries the token.
        """
        agent = manifest.name
        # 같은 **레플리카**를 두 번 띄우면 첫 핸들을 덮어 컨테이너가 미아가 된다.
        # 다른 레플리카는 별개의 컨테이너이므로 허용한다 (01 Scalability)
        if (agent, replica) in self.launched:
            raise MalkuthError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_001,
                message="agent replica is already launched",
                agent=agent,
                details={"replica": replica},
            )

        # 03 Rule 2 — 포트는 runtime 이 준다. 호출자가 명시하면 그것을 존중하고,
        # 아니면 범위에서 할당한다. A2A 가 꺼진 에이전트는 범위를 소비하지 않는다
        if a2a_port is None and self.ports is not None and manifest.spec.a2a.enabled:
            a2a_port = self.ports.allocate(agent, replica=replica)

        spec = build_container_spec(
            manifest,
            env=_with_memory(authenticated_env(self.issuer, agent, secrets), memory),
            replica=replica,
            a2a_port=a2a_port,
            network=self.engine.network,
        )

        # 02 Lifecycle — 이미지는 배포 파이프라인이 굽는다 (Rule 1). runtime 이
        # 보는 것은 Built 이후다
        lifecycle = AgentLifecycle(agent=agent)
        lifecycle.transition(AgentState.BUILT)
        lifecycle.transition(AgentState.STARTING)

        handle = await self.engine.start(spec)
        client = ControlClient(
            f"http://127.0.0.1:{handle.control_port}",
            agent=agent,
            # 05 Retry Layering — runtime 이 재시도 주체다 (읽기만)
            retry=NETWORK_RETRY,
            # 컨테이너에 주입한 것과 같은 토큰 — 발급자가 하나이므로 일치한다
            token=self.issuer.issue(agent),
        )

        launched = LaunchedAgent(
            agent=agent, handle=handle, client=client, replica=replica, lifecycle=lifecycle
        )
        self.launched[agent, replica] = launched
        self._watch(launched)
        log.info(
            "agent launched",
            agent=agent,
            container_id=handle.short_id,
            image=handle.image,
            port=handle.control_port,
        )
        return launched

    def _watch(self, launched: LaunchedAgent) -> None:
        """Start this replica's health loop, when supervision is on.

        02 Lifecycle Rules 3 — Ready 인 에이전트는 주기적으로 확인한다.

        **첫 성공이 Ready 로 올린다**: 02 Rule 2 는 "기동 → initialize() →
        health OK 가 되어야 그래프에 attach" 라고 규정한다. 기동만으로 Ready 를
        선언하면 initialize 가 끝내 실패한 컨테이너가 태스크를 받는다.
        기동 성공 판정은 **runtime 이** 하므로 monitor 가 아니라 여기서 올린다.
        """
        if self.health_interval_s is None:
            return

        from malkuth.runtime.health import HealthMonitor

        monitor = HealthMonitor(
            agent=launched.agent,
            probe=launched.client,
            lifecycle=launched.lifecycle,
            interval_s=self.health_interval_s,
            metrics=self.metrics,
            sleep=self.health_sleep,
            on_state=lambda state: self._promote(launched, state),
        )
        self._monitors[launched.agent, launched.replica] = asyncio.create_task(
            self._poll(launched, monitor)
        )

    async def _poll(self, launched: LaunchedAgent, monitor: Any) -> None:
        """Drive one replica's health loop until it is stopped.

        레플리카 하나의 health 루프를 정지될 때까지 돌립니다.

        루프가 예외로 죽으면 그 레플리카는 **영원히 감시되지 않는다** —
        조용히 사라지지 않도록 남기고, 다른 레플리카는 계속 돈다.
        """
        try:
            await monitor.run()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.error(
                "agent health loop stopped",
                agent=launched.agent,
                container_id=launched.handle.short_id,
                exc_info=err,
            )

    async def _unwatch(self, launched: LaunchedAgent) -> None:
        """이 레플리카의 health 루프를 멈춘다 — 정지한 컨테이너를 계속 두드리지 않는다."""
        task = self._monitors.pop((launched.agent, launched.replica), None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _promote(self, launched: LaunchedAgent, state: AgentState) -> None:
        """첫 health 성공을 Ready 로 올린다.

        02 Rule 2 — 기동만으로 Ready 를 선언하면 `initialize()` 가 끝내
        실패한 컨테이너가 태스크를 받는다. 그 판정은 monitor 가 아니라
        **runtime 이** 한다 (lifecycle 은 성공을 Ready 로 올리지 않는다).
        """
        if state is AgentState.STARTING:
            launched.lifecycle.transition(AgentState.READY)

    def replicas_of(self, agent: str) -> list[LaunchedAgent]:
        """이 에이전트의 기동된 레플리카 — replica 순서로."""
        return [launched for (name, _), launched in sorted(self.launched.items()) if name == agent]

    def route(self, agent: str) -> ControlClient:
        """Pick the next replica's client, round-robin.

        레플리카 간 round-robin 으로 클라이언트를 고릅니다 (01 Scalability).

        **health 를 보지 않는다**: `ReplicaRouter` 가 그 역할이지만
        `AgentLifecycle` 이 프로덕션에서 돌지 않아 모든 레플리카가
        `DECLARED` 에 머문다 — 그대로 쓰면 항상 `RT_009` 다 (#213).

        Raises:
            MalkuthError: RUNTIME/``RT_009`` if the agent has no launched replica.
        """
        replicas = self.replicas_of(agent)
        if not replicas:
            raise MalkuthError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_009,
                message="no launched replica for agent",
                agent=agent,
                retryable=True,
            )
        cursor = self._cursors.get(agent, 0)
        self._cursors[agent] = cursor + 1
        return replicas[cursor % len(replicas)].client

    async def stop(self, agent: str, *, replica: int | None = None) -> None:
        """Stop an agent's replicas and forget its token.

        에이전트를 정지하고 토큰을 버립니다 — 죽은 토큰을 들고 있지 않습니다.
        컨테이너 정지가 실패하면 핸들을 **그대로 둡니다** — 유일한 재시도
        수단을 먼저 버리면 미아 컨테이너를 다시 정리할 방법이 없습니다.

        Args:
            agent: The agent to stop.
            replica: One replica, or **every** replica when omitted — 02 의
                drain 은 에이전트 단위이므로 전부가 기본이다.
        """
        targets = (
            [self.launched[agent, replica]]
            if replica is not None and (agent, replica) in self.launched
            else self.replicas_of(agent)
        )
        for launched in targets:
            await self._unwatch(launched)
            # 02 Lifecycle 5 — 정지 전에 새 태스크 수락을 멈춘다.
            # STARTING 에서 바로 멈추는 경우도 있어 Draining 을 강요하지 않는다
            if launched.lifecycle.state is AgentState.READY:
                launched.lifecycle.transition(AgentState.DRAINING)
            await launched.aclose()
            await self.engine.stop(launched.handle)
            launched.lifecycle.transition(AgentState.STOPPED)
            del self.launched[agent, launched.replica]
            if self.ports is not None:
                self.ports.release(agent, replica=launched.replica)

        # 토큰은 에이전트 단위다 — 레플리카가 남아 있으면 아직 버리지 않는다
        if not self.replicas_of(agent):
            self.issuer.forget(agent)
            self._cursors.pop(agent, None)

    async def stop_all(self) -> None:
        """기동된 에이전트를 전부 정지한다 — 하나가 실패해도 나머지를 계속 정리한다."""
        failures: list[BaseException] = []
        for agent in dict.fromkeys(name for name, _ in self.launched):
            try:
                await self.stop(agent)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                failures.append(err)
                log.error("agent stop failed", agent=agent, exc_info=err)

        if failures:
            raise MalkuthError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_005,
                message="one or more agents failed to stop",
                details={"agents": len(failures)},
            ) from failures[0]

    def clients(self) -> dict[str, ControlClient]:
        """에이전트별 대표 클라이언트 — **레플리카가 여럿이면 라우팅되지 않는다.**

        분산이 필요하면 `route` 를 쓴다: 이 매핑은 값이 고정이라 같은 레플리카만
        계속 부른다.
        """
        return {name: replicas[0].client for name, replicas in self._by_agent().items()}

    def _by_agent(self) -> dict[str, list[LaunchedAgent]]:
        """에이전트별 레플리카 목록."""
        grouped: dict[str, list[LaunchedAgent]] = {}
        for (name, _), launched in sorted(self.launched.items()):
            grouped.setdefault(name, []).append(launched)
        return grouped


__all__ = ["AgentLauncher", "LaunchedAgent"]
