"""Periodic health monitoring for agent containers.

주기 health check 와 circuit breaker. 시간 판정은 전부 주입된 clock/sleep 을 쓴다 —
테스트가 실제로 자면 느려지고 비결정적이 된다 (06 Testing 2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from malkuth.core.agent import HealthState
from malkuth.core.errors import CircuitBreaker, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from malkuth.core.agent import HealthStatus
    from malkuth.observability.metrics import Metrics
    from malkuth.runtime.lifecycle import AgentLifecycle, AgentState

DEFAULT_INTERVAL_S = 10.0
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_UNHEALTHY_THRESHOLD = 3

log = structlog.get_logger(__name__)


@runtime_checkable
class HealthProbe(Protocol):
    """Asks one agent for its health.

    에이전트 하나의 상태를 묻는 계약 — Control API 클라이언트가 이를 구현한다.
    """

    async def health(self) -> HealthStatus:
        """``/v1/health`` 를 호출한다."""
        ...


@dataclass
class HealthMonitor:
    """Polls one agent and feeds its lifecycle.

    에이전트 하나를 주기적으로 확인하고 결과를 lifecycle 에 넘긴다.
    연속 실패가 임계를 넘으면 Unhealthy 로 전이된다.
    """

    agent: str
    probe: HealthProbe
    lifecycle: AgentLifecycle
    interval_s: float = DEFAULT_INTERVAL_S
    timeout_s: float = DEFAULT_TIMEOUT_S
    unhealthy_threshold: int = DEFAULT_UNHEALTHY_THRESHOLD
    metrics: Metrics | None = None
    breaker: CircuitBreaker | None = None
    sleep: Callable[[float], object] | None = None

    consecutive_failures: int = field(default=0, init=False)
    last_status: HealthState | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.breaker is None:
            self.breaker = CircuitBreaker(
                target=f"control:{self.agent}",
                open_category=ErrorCategory.RUNTIME,
                open_code=ErrorCode.RT_002,
            )

    async def check_once(self) -> AgentState:
        """Run one health check and record the outcome.

        한 번 확인하고 결과를 기록합니다. 회로가 열려 있으면 **호출하지 않고**
        실패로 간주합니다 — 응답 없는 에이전트를 계속 두드리면 runtime 이
        그 대기에 묶입니다.

        Returns:
            The lifecycle state after recording this result.
        """
        assert self.breaker is not None  # noqa: S101 — __post_init__ 가 보장

        if not self.breaker.can_attempt():
            return self._record(healthy=False, reason="circuit open")

        try:
            status = await asyncio.wait_for(self.probe.health(), timeout=self.timeout_s)
        except (TimeoutError, Exception) as err:  # noqa: B014 — TimeoutError 를 명시해 의도를 남긴다
            self.breaker.record_failure()
            return self._record(healthy=False, reason=type(err).__name__)

        self.breaker.record_success()
        self.last_status = status.status
        # degraded 는 살아있는 상태다 — optional 자원이 빠졌을 뿐이라 재시작 대상이 아니다
        return self._record(healthy=status.status is not HealthState.UNHEALTHY)

    def _record(self, *, healthy: bool, reason: str | None = None) -> AgentState:
        """결과를 lifecycle 과 메트릭에 반영한다."""
        if healthy:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            log.warning(
                "agent health check failed",
                agent=self.agent,
                attempt=self.consecutive_failures,
                max_attempts=self.unhealthy_threshold,
                reason=reason,
            )

        state = self.lifecycle.record_health(healthy=healthy, threshold=self.unhealthy_threshold)
        if self.metrics is not None:
            self.metrics.gauge("malkuth_agent_health").labels(agent=self.agent).set(
                1 if healthy else 0
            )
        return state

    async def run(self, *, iterations: int | None = None) -> None:
        """Poll until stopped.

        정지될 때까지 주기적으로 확인합니다.

        Args:
            iterations: Stop after this many checks; None polls forever.
                테스트는 유한 횟수를 주어 루프를 끝냅니다.
        """
        sleeper = self.sleep or asyncio.sleep
        count = 0
        while iterations is None or count < iterations:
            await self.check_once()
            count += 1
            if iterations is None or count < iterations:
                result = sleeper(self.interval_s)
                if asyncio.iscoroutine(result):
                    await result


def track_running(metrics: Metrics, agent: str, *, running: bool) -> None:
    """Update the running-container gauge.

    실행 중 컨테이너 게이지를 갱신합니다.
    """
    metrics.gauge("malkuth_containers_running").labels(agent=agent).set(1 if running else 0)


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_UNHEALTHY_THRESHOLD",
    "HealthMonitor",
    "HealthProbe",
    "track_running",
]
