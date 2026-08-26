"""Run submission.

그래프 하나를 실제로 굴리는 경로. 토폴로지 · 노드 런타임 · checkpointer 를 묶어
mission run 을 완주시키고, service run 은 iteration 을 이어간다.

Run 슬롯은 ``RunManager`` 가 관리하며, **슬롯 반납을 finally 로 보장한다** —
예외 경로에서 놓치면 슬롯이 영원히 점유된 채 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.builder import build_graph
from malkuth.orchestrator.run import RunManager, RunStatus
from malkuth.orchestrator.topology import GraphMode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from malkuth.orchestrator.builder import NodeRuntime
    from malkuth.orchestrator.run import RunHandle
    from malkuth.orchestrator.topology import GraphTopology

DEFAULT_NODE_TIMEOUT_S = 300.0

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunResult:
    """The outcome of one submitted run.

    제출된 run 하나의 결과.
    """

    run_id: str
    graph: str
    mode: GraphMode
    status: RunStatus
    state: dict[str, Any] = field(default_factory=dict)
    error: MalkuthError | None = None

    @property
    def ok(self) -> bool:
        """완료 상태인지."""
        return self.status is RunStatus.COMPLETED


def _thread_config(run_id: str) -> dict[str, Any]:
    """checkpointer 의 thread 를 run 에 고정한다 — 재개가 같은 흐름을 잇도록."""
    return {"configurable": {"thread_id": run_id}}


def seed_state(
    initial_state: Mapping[str, Any], *, run_id: str, trace_id: str | None = None
) -> dict[str, Any]:
    """Inject the reserved run channels into the initial state.

    예약 채널(``_run_id`` / ``_trace_id``)을 초기 state 에 주입합니다.

    이걸 빠뜨리면 각 노드 태스크가 ``run-unknown`` 으로 실행되어, 슬롯이 쥔
    실제 run_id 와 로그·trace 가 어긋납니다 — run_id 하나로 전 계층 로그를
    잇는다는 05 의 추적 전제가 깨집니다.

    Args:
        initial_state: The caller's starting state.
        run_id: The run this graph execution belongs to.
        trace_id: Distributed trace id; defaults to the run id.

    Returns:
        The state with reserved channels populated.
    """
    return {**dict(initial_state), "_run_id": run_id, "_trace_id": trace_id or run_id}


@dataclass
class RunSubmitter:
    """Submits graph runs and tracks their slots.

    그래프 run 을 제출하고 슬롯을 추적한다.

    Attributes:
        runtime: Invokes each node's agent.
        manager: Owns run slots — mission/service 를 분리 관리한다.
        checkpointer: Persists state so a run can resume.
    """

    runtime: NodeRuntime
    manager: RunManager = field(default_factory=RunManager)
    checkpointer: BaseCheckpointSaver[Any] | None = None
    node_timeout_s: float = DEFAULT_NODE_TIMEOUT_S

    async def submit(
        self,
        topology: GraphTopology,
        initial_state: Mapping[str, Any],
        *,
        run_id: str | None = None,
    ) -> RunResult:
        """Run a mission graph to completion.

        mission 그래프를 완주시킵니다.

        Args:
            topology: The validated graph topology.
            initial_state: The starting state.
            run_id: Explicit run id; generated when omitted.

        Returns:
            The run result — ``ok`` is False when the run failed.

        Raises:
            MalkuthError: GRAPH/``GRAPH_001`` when a service graph is submitted
                here — 상주 그래프는 완주 개념이 없으므로 ``resume`` 로 이어갑니다.
        """
        if topology.spec.mode is not GraphMode.MISSION:
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="only mission graphs can be submitted for completion",
                details={"graph": topology.metadata.name, "mode": str(topology.spec.mode)},
            )

        handle = self.manager.acquire(topology, run_id=run_id or f"run-{uuid4().hex[:12]}")
        return await self._drive(topology, handle, initial_state)

    async def resume(
        self, topology: GraphTopology, run_id: str, initial_state: Mapping[str, Any] | None = None
    ) -> RunResult:
        """Continue a run from its last checkpoint.

        마지막 checkpoint 에서 run 을 이어갑니다. checkpointer 가 없으면
        이어갈 지점이 없으므로 거부합니다 — 조용히 처음부터 다시 돌면
        부수효과가 두 번 일어납니다.

        Raises:
            MalkuthError: STORAGE/``STOR_002`` when no checkpointer is attached.
        """
        if self.checkpointer is None:
            raise MalkuthError(
                category=ErrorCategory.STORAGE,
                code=ErrorCode.STOR_002,
                message="cannot resume without a checkpointer",
                details={"run_id": run_id, "graph": topology.metadata.name},
            )

        handle = self.manager.acquire(topology, run_id=run_id)
        return await self._drive(topology, handle, initial_state or {})

    async def _drive(
        self,
        topology: GraphTopology,
        handle: RunHandle,
        initial_state: Mapping[str, Any],
    ) -> RunResult:
        """그래프를 실행하고 슬롯을 반드시 반납한다."""
        graph = build_graph(
            topology,
            self.runtime,
            checkpointer=self.checkpointer,
            node_timeout_s=self.node_timeout_s,
        )
        bound = log.bind(graph=topology.metadata.name, run_id=handle.run_id)
        outcome = RunStatus.FAILED

        try:
            final = await graph.ainvoke(
                seed_state(initial_state, run_id=handle.run_id),
                config=_thread_config(handle.run_id),
            )
        except MalkuthError as err:
            bound.error("graph run failed", error_code=err.code, mode=str(topology.spec.mode))
            return RunResult(
                run_id=handle.run_id,
                graph=topology.metadata.name,
                mode=topology.spec.mode,
                status=RunStatus.FAILED,
                error=err,
            )
        else:
            outcome = RunStatus.COMPLETED
            bound.info("graph run completed", mode=str(topology.spec.mode))
            return RunResult(
                run_id=handle.run_id,
                graph=topology.metadata.name,
                mode=topology.spec.mode,
                status=RunStatus.COMPLETED,
                state=dict(final),
            )
        finally:
            # 슬롯 반납을 finally 로 보장한다 — 예외 경로에서 놓치면 슬롯이
            # 영원히 점유된 채 남는다. handle.status 는 RUNNING 그대로이므로
            # 실제 결과(outcome)로 반납해야 상태가 거짓말하지 않는다
            self.manager.release(handle.run_id, outcome)


__all__ = ["DEFAULT_NODE_TIMEOUT_S", "RunResult", "RunSubmitter", "seed_state"]
