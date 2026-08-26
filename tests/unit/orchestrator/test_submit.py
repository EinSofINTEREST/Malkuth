"""Unit tests for run submission.

컨테이너 없이 검증한다 — 이 계층의 계약은 슬롯 관리와 결과 확정이지
노드 실행 그 자체가 아니다.
"""

from __future__ import annotations

import pytest

from malkuth.core.agent import TaskResult
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.checkpoint import build_checkpointer
from malkuth.orchestrator.run import RunStatus
from malkuth.orchestrator.submit import RunSubmitter, seed_state
from malkuth.orchestrator.topology import GraphMode
from tests.fixtures.topologies import make_mission, make_service


class ScriptedRuntime:
    """노드별 출력을 스크립트하는 런타임 대역."""

    def __init__(self, outputs: dict[str, dict] | None = None) -> None:
        self._outputs = outputs or {}
        self.invoked: list[str] = []

    async def invoke(self, node, task):
        self.invoked.append(node.id)
        return TaskResult.completed(task, output=self._outputs.get(node.id, {}))


class RecordingRuntime:
    """노드가 받은 태스크의 추적 정보를 기록하는 런타임 대역."""

    def __init__(self) -> None:
        self.tasks: list[tuple[str, str, str]] = []

    async def invoke(self, node, task):
        self.tasks.append((node.id, task.run_id, task.trace.trace_id))
        return TaskResult.completed(task, output={})


class FailingRuntime:
    """항상 실패하는 런타임 대역."""

    def __init__(self, code: ErrorCode = ErrorCode.GRAPH_002) -> None:
        self._code = code

    async def invoke(self, node, task):
        raise MalkuthError(category=ErrorCategory.GRAPH, code=self._code, message="node failed")


def submitter(runtime=None, **overrides) -> RunSubmitter:
    return RunSubmitter(
        runtime=runtime or ScriptedRuntime(),
        checkpointer=overrides.pop("checkpointer", build_checkpointer("memory")),
        **overrides,
    )


# --- mission run ---------------------------------------------------------------


async def test_mission_run_reaches_the_end():
    runtime = ScriptedRuntime({"planner": {"plan": "p"}, "researcher": {"findings": ["f"]}})
    submit = submitter(runtime)

    result = await submit.submit(make_mission(), {"query": "q"})

    assert result.ok
    assert result.status is RunStatus.COMPLETED
    assert runtime.invoked == ["planner", "researcher"]


async def test_only_declared_output_keys_are_merged():
    """노드가 state 전체를 덮어쓰는 패턴은 금지된다 — output_map 에 선언된
    키만 병합된다 (04 State Schema)."""
    runtime = ScriptedRuntime({"planner": {"plan": "three steps", "sneaky": "x"}})

    result = await submitter(runtime).submit(make_mission(), {"query": "q"})

    # fixture 토폴로지는 output_map 을 선언하지 않으므로 아무것도 병합되지 않는다
    assert "sneaky" not in result.state
    assert result.state["query"] == "q"


async def test_declared_output_map_merges_into_state():
    """output_map 을 선언하면 그 키가 state 에 실린다."""
    nodes = [
        {
            "id": "planner",
            "agent": "agents/planner@0.1.0",
            "output_map": {"plan": "output.plan"},
        },
    ]
    topology = make_mission(
        nodes=nodes,
        edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
    )
    runtime = ScriptedRuntime({"planner": {"plan": "three steps"}})

    result = await submitter(runtime).submit(topology, {"query": "q"})

    assert result.state["plan"] == "three steps"


async def test_run_id_can_be_supplied():
    """재개하려면 호출자가 run_id 를 알아야 한다."""
    result = await submitter().submit(make_mission(), {"query": "q"}, run_id="run-fixed")

    assert result.run_id == "run-fixed"


async def test_generated_run_ids_are_unique():
    submit = submitter()

    first = await submit.submit(make_mission(), {"query": "q"})
    second = await submit.submit(make_mission(), {"query": "q"})

    assert first.run_id != second.run_id


# --- 실패 경로 ------------------------------------------------------------------


async def test_node_failure_produces_a_failed_result():
    """실패를 예외로 흘리면 호출자가 슬롯 상태를 알 수 없다."""
    result = await submitter(FailingRuntime()).submit(make_mission(), {"query": "q"})

    assert not result.ok
    assert result.status is RunStatus.FAILED
    assert result.error is not None


async def test_failure_carries_the_error_code():
    result = await submitter(FailingRuntime()).submit(make_mission(), {"query": "q"})

    assert result.error is not None
    assert result.error.code == "GRAPH_002"


# --- 슬롯 관리 ------------------------------------------------------------------


async def test_completed_run_releases_its_slot():
    submit = submitter()

    result = await submit.submit(make_mission(), {"query": "q"})

    assert submit.manager.runs[result.run_id].status is RunStatus.COMPLETED
    assert submit.manager.active(GraphMode.MISSION) == 0


async def test_failed_run_also_releases_its_slot():
    """예외 경로에서 놓치면 슬롯이 영원히 점유된 채 남는다."""
    submit = submitter(FailingRuntime())

    result = await submit.submit(make_mission(), {"query": "q"})

    assert submit.manager.active(GraphMode.MISSION) == 0
    assert submit.manager.runs[result.run_id].status is RunStatus.FAILED


async def test_released_status_is_not_left_as_running():
    """handle.status 를 그대로 반납하면 모든 run 이 running 으로 남아 거짓말한다."""
    submit = submitter(FailingRuntime())

    result = await submit.submit(make_mission(), {"query": "q"})

    assert submit.manager.runs[result.run_id].status is not RunStatus.RUNNING


# --- 모드 규칙 ------------------------------------------------------------------


async def test_service_graph_cannot_be_submitted_for_completion():
    """상주 그래프는 완주 개념이 없다."""
    with pytest.raises(MalkuthError) as exc_info:
        await submitter().submit(make_service(), {})

    assert exc_info.value.code == "GRAPH_001"


# --- 재개 ----------------------------------------------------------------------


async def test_resume_without_a_checkpointer_is_rejected():
    """조용히 처음부터 다시 돌면 부수효과가 두 번 일어난다."""
    submit = submitter(checkpointer=None)

    with pytest.raises(MalkuthError) as exc_info:
        await submit.resume(make_mission(), "run-1")

    assert exc_info.value.code == "STOR_002"


async def test_resume_reuses_the_run_id():
    """재개는 같은 run_id 로 이어져야 checkpoint 흐름이 연결된다."""
    submit = submitter()

    result = await submit.resume(make_mission(), "run-resume")

    assert result.run_id == "run-resume"


async def test_resuming_a_tracked_run_is_rejected():
    """이미 추적 중인 run 을 다시 acquire 하면 슬롯 회계가 어긋난다.

    재개는 프로세스가 재시작되어 추적이 비어 있는 상태를 전제로 한다.
    """
    submit = submitter()
    await submit.submit(make_mission(), {"query": "q"}, run_id="run-live")

    with pytest.raises(MalkuthError) as exc_info:
        await submit.resume(make_mission(), "run-live")

    assert exc_info.value.code == "VAL_002"


# --- run 추적 정보 전파 ---------------------------------------------------------


async def test_nodes_receive_the_actual_run_id():
    """예약 채널을 주입하지 않으면 노드 태스크가 run-unknown 으로 실행되어,
    run_id 하나로 전 계층 로그를 잇는다는 05 의 추적 전제가 깨진다."""
    runtime = RecordingRuntime()

    result = await submitter(runtime).submit(make_mission(), {"query": "q"}, run_id="run-actual")

    assert runtime.tasks
    assert all(run_id == result.run_id for _, run_id, _ in runtime.tasks)


async def test_nodes_receive_a_trace_id():
    runtime = RecordingRuntime()

    await submitter(runtime).submit(make_mission(), {"query": "q"}, run_id="run-traced")

    assert all(trace_id == "run-traced" for _, _, trace_id in runtime.tasks)


def test_seed_state_populates_the_reserved_channels():
    seeded = seed_state({"query": "q"}, run_id="run-1")

    assert seeded["_run_id"] == "run-1"
    assert seeded["_trace_id"] == "run-1"
    assert seeded["query"] == "q"


def test_seed_state_accepts_a_distinct_trace_id():
    """A2A 로 이어지는 trace 는 run 과 다른 id 를 가질 수 있다."""
    seeded = seed_state({}, run_id="run-1", trace_id="trace-9")

    assert seeded["_trace_id"] == "trace-9"


def test_seed_state_does_not_mutate_the_caller_state():
    original = {"query": "q"}

    seed_state(original, run_id="run-1")

    assert original == {"query": "q"}
