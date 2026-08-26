"""Docker SDK call layer.

Docker 를 실제로 만지는 유일한 계층. 상위(orchestrator)는 이 계약만 알고,
SDK 타입은 여기서 벗겨진다 — backend 를 Kubernetes 로 바꿔도 계약은 그대로다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from malkuth.runtime.docker.errors import (
    OOM_EXIT_CODE,
    drain_timeout,
    image_unavailable,
    oom_killed,
    start_failed,
)
from malkuth.runtime.spec import ContainerSpec

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_NETWORK = "malkuth-net"
DEFAULT_STOP_GRACE_S = 30.0
DEFAULT_DRAIN_TIMEOUT_S = 30.0

log = structlog.get_logger(__name__)


AGENT_LABEL = "malkuth.agent"
CONTROL_PORT_NAME = "control"


def agent_of(spec: ContainerSpec) -> str:
    """Read the agent name from the spec's labels.

    스펙의 라벨에서 에이전트 이름을 읽습니다 — 라벨이 곧 소유자 표시이므로
    별도 필드를 만들지 않습니다.
    """
    return spec.labels.get(AGENT_LABEL, spec.name)


def control_port_of(spec: ContainerSpec) -> int:
    """Find the control port declared in the spec.

    스펙이 선언한 control 포트를 찾습니다.

    Raises:
        MalkuthError: RUNTIME/``RT_001`` if no control port is declared —
            control 포트가 없으면 runtime 이 에이전트에 닿을 수 없습니다.
    """
    for binding in spec.ports:
        if binding.name == CONTROL_PORT_NAME:
            return binding.container_port
    raise start_failed(agent_of(spec), spec.image, reason="spec declares no control port")


@runtime_checkable
class DockerClient(Protocol):
    """The slice of the Docker SDK this layer uses.

    이 계층이 쓰는 Docker SDK 표면. 좁게 잡아 테스트 대역이 가벼워지고,
    SDK 변경의 영향 범위가 여기로 한정된다.
    """

    def ensure_image(self, image: str) -> None:
        """이미지를 확보한다 — 없으면 pull."""
        ...

    def ensure_network(self, name: str) -> None:
        """네트워크를 확보한다 — 없으면 생성."""
        ...

    def create(self, **kwargs: Any) -> str:
        """컨테이너를 만들고 id 를 돌려준다."""
        ...

    def start(self, container_id: str) -> None:
        """컨테이너를 기동한다."""
        ...

    def inspect(self, container_id: str) -> dict[str, Any]:
        """컨테이너 상태를 조회한다."""
        ...

    def port_of(self, container_id: str, container_port: int) -> int:
        """컨테이너 포트에 매핑된 호스트 포트."""
        ...

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        """SIGTERM 후 유예 시간이 지나면 SIGKILL."""
        ...

    def remove(self, container_id: str) -> None:
        """컨테이너를 제거한다."""
        ...


@dataclass(frozen=True)
class ContainerHandle:
    """A started container.

    기동된 컨테이너. 로그·메트릭의 표준 필드를 그대로 담는다.
    """

    agent: str
    container_id: str
    image: str
    control_port: int

    @property
    def short_id(self) -> str:
        """로그에 싣는 짧은 id."""
        return self.container_id[:12]


@dataclass
class DockerEngine:
    """Starts and stops agent containers.

    에이전트 컨테이너를 기동/정지한다. 순수 로직(스펙 생성, 상태 머신)은
    이미 분리되어 있으므로 여기서는 SDK 호출과 에러 변환만 담당한다.
    """

    client: DockerClient
    network: str = DEFAULT_NETWORK
    stop_grace_s: float = DEFAULT_STOP_GRACE_S
    started: dict[str, ContainerHandle] = field(default_factory=dict)

    async def start(self, spec: ContainerSpec) -> ContainerHandle:
        """Create and start one agent container.

        컨테이너를 만들고 기동합니다.

        Args:
            spec: The container specification derived from the manifest.

        Returns:
            The started container handle.

        Raises:
            MalkuthError: RUNTIME/``RT_004`` if the image is unavailable,
                ``RT_001`` if the container fails to create or start.
        """
        agent = agent_of(spec)
        container_control_port = control_port_of(spec)

        try:
            await asyncio.to_thread(self.client.ensure_image, spec.image)
        except Exception as err:
            raise image_unavailable(agent, spec.image, reason=type(err).__name__) from err

        try:
            await asyncio.to_thread(self.client.ensure_network, self.network)
        except Exception as err:
            raise start_failed(
                agent, spec.image, reason=f"network unavailable: {type(err).__name__}"
            ) from err

        kwargs = spec.to_docker_kwargs()
        kwargs["network"] = self.network

        try:
            container_id = await asyncio.to_thread(self.client.create, **kwargs)
        except Exception as err:
            raise start_failed(agent, spec.image, reason=type(err).__name__) from err

        try:
            await asyncio.to_thread(self.client.start, container_id)
            port = await asyncio.to_thread(
                self.client.port_of, container_id, container_control_port
            )
        except Exception as err:
            # 반쯤 만들어진 컨테이너를 남기면 유령 컨테이너가 쌓인다
            await self._discard(container_id)
            raise start_failed(
                agent, spec.image, container_id=container_id[:12], reason=type(err).__name__
            ) from err

        handle = ContainerHandle(
            agent=agent, container_id=container_id, image=spec.image, control_port=port
        )
        self.started[agent] = handle
        log.info(
            "agent container started",
            agent=agent,
            container_id=handle.short_id,
            image=spec.image,
            port=port,
        )
        return handle

    async def inspect(self, handle: ContainerHandle) -> dict[str, Any]:
        """컨테이너 상태를 조회한다."""
        state: dict[str, Any] = await asyncio.to_thread(self.client.inspect, handle.container_id)
        return state

    async def check_exit(self, handle: ContainerHandle) -> None:
        """Raise if the container died in a way worth distinguishing.

        컨테이너가 죽은 방식을 구분해 올립니다 — OOM 은 리소스 조정이 필요하고,
        일반 종료는 재시작으로 풀릴 수 있어 정책이 다릅니다.

        Raises:
            MalkuthError: RUNTIME/``RT_003`` when the container was OOM killed.
        """
        state = await self.inspect(handle)
        if state.get("OOMKilled") or state.get("ExitCode") == OOM_EXIT_CODE:
            raise oom_killed(
                handle.agent,
                handle.short_id,
                image=handle.image,
                exit_code=state.get("ExitCode"),
            )

    async def drain(
        self,
        handle: ContainerHandle,
        *,
        request_drain: Callable[[], Any],
        inflight: Callable[[], int],
        timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        poll_s: float = 0.5,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Stop accepting tasks and wait for in-flight work.

        새 태스크 수락을 멈추고 진행 중 태스크를 기다립니다.
        시간 판정은 주입 가능한 clock/sleep 을 씁니다 — 테스트가 실제로 자지
        않도록 하기 위해서입니다.

        Args:
            handle: The container to drain.
            request_drain: Asks the agent to stop accepting tasks.
                Must be awaited if it returns an awaitable.
            inflight: Reports how many tasks are still running.
            timeout_s: How long to wait before giving up.
            poll_s: Delay between checks.
            sleep: Delay function; defaults to ``asyncio.sleep``.
            clock: Monotonic clock; defaults to the event loop clock.

        Raises:
            MalkuthError: RUNTIME/``RT_005`` if work is still running at the
                deadline — 남은 태스크를 조용히 버리지 않습니다.
        """
        sleeper = sleep or asyncio.sleep
        now = clock or asyncio.get_running_loop().time

        result = request_drain()
        if asyncio.iscoroutine(result):
            await result

        deadline = now() + timeout_s
        while True:
            remaining = inflight()
            if remaining == 0:
                log.info(
                    "agent drained",
                    agent=handle.agent,
                    container_id=handle.short_id,
                )
                return
            if now() >= deadline:
                raise drain_timeout(
                    handle.agent,
                    handle.short_id,
                    image=handle.image,
                    inflight=remaining,
                    timeout_s=timeout_s,
                )
            await sleeper(poll_s)

    async def stop(self, handle: ContainerHandle, *, remove: bool = True) -> None:
        """Stop a container, SIGTERM then SIGKILL.

        컨테이너를 정지합니다 — SIGTERM 후 유예 시간이 지나면 SIGKILL.
        정지 경로는 실패해도 진행합니다: 남은 컨테이너가 유령으로 쌓이는 편이
        더 나쁩니다.
        """
        try:
            await asyncio.to_thread(
                self.client.stop, handle.container_id, timeout_s=self.stop_grace_s
            )
        except Exception as err:  # noqa: BLE001 — 정지 실패가 정리를 막으면 안 된다
            log.warning(
                "container stop failed",
                agent=handle.agent,
                container_id=handle.short_id,
                image=handle.image,
                error=type(err).__name__,
            )

        if remove:
            await self._discard(handle.container_id)

        self.started.pop(handle.agent, None)
        log.info(
            "agent container stopped",
            agent=handle.agent,
            container_id=handle.short_id,
            image=handle.image,
        )

    async def _discard(self, container_id: str) -> None:
        """컨테이너를 제거한다 — 실패는 로그만 남긴다."""
        try:
            await asyncio.to_thread(self.client.remove, container_id)
        except Exception as err:  # noqa: BLE001
            log.warning(
                "container removal failed",
                container_id=container_id[:12],
                error=type(err).__name__,
            )


__all__ = [
    "DEFAULT_DRAIN_TIMEOUT_S",
    "DEFAULT_NETWORK",
    "DEFAULT_STOP_GRACE_S",
    "AGENT_LABEL",
    "CONTROL_PORT_NAME",
    "ContainerHandle",
    "DockerClient",
    "DockerEngine",
    "agent_of",
    "control_port_of",
]
