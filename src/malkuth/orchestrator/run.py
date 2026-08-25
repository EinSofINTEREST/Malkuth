"""Run execution — mission runs and the service iteration loop.

Run 실행. mission 은 END 도달로 종료하고, service 는 iteration 을 무한 반복한다.

Service 루프의 시간 의존 동작(idle backoff)은 주입된 sleep/clock 을 통해서만
수행한다 — 테스트가 실제 sleep 없이 결정적으로 검증할 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.topology import GraphMode, GraphTopology

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

DEFAULT_MAX_CONCURRENT_RUNS = 10
DEFAULT_MAX_SERVICE_RUNS = 5

_RUN_ID_KEY = "_run_id"
_TRACE_ID_KEY = "_trace_id"


class RunStatus(StrEnum):
    """Run 진행 상태.

    ``halted`` 는 service run 이 연속 실패 임계를 넘어 정지한 상태다 (``GRAPH_005``).
    """

    RUNNING = "running"
    DRAINING = "draining"
    COMPLETED = "completed"
    FAILED = "failed"
    HALTED = "halted"
    STOPPED = "stopped"


@dataclass
class RunHandle:
    """A live run.

    실행 중인 run 의 핸들. drain 요청과 상태 조회 창구다.
    """

    run_id: str
    graph: str
    mode: GraphMode
    status: RunStatus = RunStatus.RUNNING
    iteration: int = 0
    failure_streak: int = 0
    state: dict[str, Any] = field(default_factory=dict)
    error: MalkuthError | None = None

    _drain: bool = field(default=False, init=False)

    def request_drain(self) -> None:
        """Ask the run to stop after the current iteration.

        진행 중 iteration 을 마친 뒤 정지하도록 요청합니다 (즉시 중단 아님).
        """
        self._drain = True
        if self.status is RunStatus.RUNNING:
            self.status = RunStatus.DRAINING

    @property
    def drain_requested(self) -> bool:
        """Drain 이 요청되었는지."""
        return self._drain


async def _no_sleep(_seconds: float) -> None:
    """기본 sleep — 테스트는 이 자리에 fake 를 주입한다."""
    await asyncio.sleep(_seconds)


class RunManager:
    """Tracks run slots for mission and service graphs.

    mission / service run 슬롯을 분리 관리한다. Service run 은 장기 점유하므로
    mission 과 같은 풀을 쓰면 상주 그래프가 전체 슬롯을 잠식한다.
    """

    def __init__(
        self,
        *,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
        max_service_runs: int = DEFAULT_MAX_SERVICE_RUNS,
    ) -> None:
        self._max_mission = max_concurrent_runs
        self._max_service = max_service_runs
        self._runs: dict[str, RunHandle] = {}

    @property
    def runs(self) -> dict[str, RunHandle]:
        """추적 중인 run 목록 (run_id → handle)."""
        return dict(self._runs)

    def active(self, mode: GraphMode) -> int:
        """해당 모드로 진행 중인 run 수."""
        return sum(
            1
            for run in self._runs.values()
            if run.mode is mode and run.status in (RunStatus.RUNNING, RunStatus.DRAINING)
        )

    def acquire(self, topology: GraphTopology, *, run_id: str | None = None) -> RunHandle:
        """Reserve a run slot for a graph.

        그래프의 run 슬롯을 확보합니다.

        Args:
            topology: The graph to run.
            run_id: Optional run id; generated when omitted.

        Returns:
            The run handle.

        Raises:
            MalkuthError: GRAPH/``GRAPH_001`` if the slot ceiling is reached.
        """
        mode = topology.mode
        limit = self._max_service if mode is GraphMode.SERVICE else self._max_mission

        if self.active(mode) >= limit:
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message=f"{mode} run slots exhausted",
                retryable=True,
                details={"graph": topology.name, "mode": str(mode), "limit": limit},
            )

        handle = RunHandle(
            run_id=run_id or f"run-{uuid.uuid4()}",
            graph=topology.name,
            mode=mode,
        )
        self._runs[handle.run_id] = handle
        return handle

    def get(self, run_id: str) -> RunHandle:
        """Look up a run by id.

        run_id 로 run 을 조회합니다.

        Raises:
            MalkuthError: NOT_FOUND/``VAL_001`` if the run is unknown.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.VAL_001,
                message=f"unknown run: {run_id}",
                details={"run_id": run_id},
            )
        return run

    def release(self, run_id: str, status: RunStatus) -> None:
        """Mark a run finished, freeing its slot.

        run 을 종료 상태로 표시해 슬롯을 반납합니다.
        """
        run = self._runs.get(run_id)
        if run is not None:
            run.status = status


class ServiceRunner:
    """Drives a service graph's perpetual iteration loop.

    상주 그래프의 iteration 루프를 구동한다. Iteration 마다 checkpoint 가 남으므로
    프로세스가 재시작되어도 다음 iteration 부터 이어서 진행할 수 있다.
    """

    def __init__(
        self,
        topology: GraphTopology,
        graph: Any,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if topology.mode is not GraphMode.SERVICE:
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="ServiceRunner requires a service-mode graph",
                details={"graph": topology.name, "mode": str(topology.mode)},
            )
        if topology.spec.service is None:  # pragma: no cover - 스키마가 이미 강제
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="service graph is missing its idle policy",
                details={"graph": topology.name},
            )

        self._topology = topology
        self._service = topology.spec.service
        self._graph = graph
        self._sleep = sleep or _no_sleep
        self.delays: list[float] = []

    async def run(
        self,
        handle: RunHandle,
        initial_state: dict[str, Any],
        *,
        max_iterations: int | None = None,
        is_idle: Callable[[dict[str, Any]], bool] | None = None,
    ) -> RunHandle:
        """Run the service loop until drained, halted, or bounded.

        Service 루프를 실행합니다. 정지 조건은 drain 요청, 연속 실패 임계 초과
        (``GRAPH_005``), 또는 테스트용 ``max_iterations`` 입니다.

        Args:
            handle: The run handle to advance.
            initial_state: State the first iteration starts from.
            max_iterations: Optional bound so tests can end the loop.
            is_idle: Predicate deciding whether an iteration found no work.

        Returns:
            The same handle, with terminal status recorded.
        """
        state = dict(initial_state)
        state.setdefault(_RUN_ID_KEY, handle.run_id)
        state.setdefault(_TRACE_ID_KEY, handle.run_id)
        idle_streak = 0

        while not handle.drain_requested:
            if max_iterations is not None and handle.iteration >= max_iterations:
                break

            try:
                state = await self._iterate(handle, state)
            except MalkuthError as err:
                if self._record_failure(handle, err):
                    return handle
                continue

            handle.failure_streak = 0
            idle_streak = await self._apply_idle_policy(state, idle_streak, is_idle)

        handle.status = RunStatus.STOPPED
        handle.state = state
        return handle

    async def _iterate(self, handle: RunHandle, state: dict[str, Any]) -> dict[str, Any]:
        """단일 iteration 실행 — iteration 단위 checkpoint 를 남긴다.

        실패하든 성공하든 iteration 은 진행한 것으로 센다 — 그래야 실패가 반복돼도
        ``max_iterations`` 경계가 유효하고, checkpoint thread 도 매번 새로 열린다.
        """
        config = {"configurable": {"thread_id": f"{handle.run_id}:{handle.iteration}"}}
        try:
            result = await self._graph.ainvoke(state, config)
        finally:
            handle.iteration += 1
        handle.state = dict(result)
        return dict(result)

    def _record_failure(self, handle: RunHandle, err: MalkuthError) -> bool:
        """실패를 누적하고, 임계 초과 시 run 을 halted 로 정지한다."""
        handle.failure_streak += 1
        if handle.failure_streak < self._service.max_failure_streak:
            return False

        handle.status = RunStatus.HALTED
        handle.error = MalkuthError(
            category=ErrorCategory.GRAPH,
            code=ErrorCode.GRAPH_005,
            message="service iteration failure streak exceeded",
            details={
                "graph": handle.graph,
                "run_id": handle.run_id,
                "iteration": handle.iteration,
                "max_failure_streak": self._service.max_failure_streak,
            },
        )
        handle.error.__cause__ = err
        return True

    async def _apply_idle_policy(
        self,
        state: dict[str, Any],
        idle_streak: int,
        is_idle: Callable[[dict[str, Any]], bool] | None,
    ) -> int:
        """작업이 없으면 backoff 하고, 작업을 찾으면 backoff 를 리셋한다."""
        if is_idle is None or not is_idle(state):
            return 0

        delay = self._service.idle.delay_for(idle_streak)
        self.delays.append(delay)
        await self._sleep(delay)
        return idle_streak + 1
