"""Graph-level tests — routing, state isolation, resume.

컨테이너 없이 그래프 라우팅을 검증한다 (06-testing.md Graph-Level Tests):
runtime 을 fake 로 치환하고 checkpointer 는 in-memory 를 쓴다.
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from malkuth.core.agent import TaskResult
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.graphs.schemas import ResearchState
from malkuth.orchestrator.builder import GraphBuilder, build_channel_schema, build_graph
from tests.fixtures.fake_runtime import FakeRuntime
from tests.fixtures.topologies import make_mission

_CONDITIONAL_EDGES = [
    {"from": "START", "to": "planner"},
    {
        "from": "planner",
        "to": "researcher",
        "condition": "malkuth.graphs.conditions:needs_research",
    },
    {"from": "planner", "to": "END", "condition": "malkuth.graphs.conditions:plan_only"},
    {"from": "researcher", "to": "END"},
]


def linear_topology():
    """planner → researcher → END 선형 그래프."""
    return make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"query": "state.query"},
                "output_map": {"plan": "output.plan"},
            },
            {
                "id": "researcher",
                "agent": "agents/researcher@0.1.0",
                "input_map": {"plan": "state.plan"},
                "output_map": {"findings": "output.findings"},
            },
        ]
    )


def conditional_topology():
    """planner 에서 조건부로 researcher 를 건너뛰는 그래프."""
    return make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "output_map": {"needs_research": "output.needs_research"},
            },
            {
                "id": "researcher",
                "agent": "agents/researcher@0.1.0",
                "output_map": {"findings": "output.findings"},
            },
        ],
        edges=_CONDITIONAL_EDGES,
    )


# --- 채널 스키마 ------------------------------------------------------------


def test_channel_schema_includes_state_fields_and_reserved_keys():
    """LangGraph 는 선언된 채널만 보존하므로 state 필드가 모두 포함돼야 한다."""
    annotations = build_channel_schema(ResearchState).__annotations__

    assert {"query", "plan", "needs_research", "findings", "report"} <= set(annotations)
    assert {"_iterations", "_run_id", "_trace_id"} <= set(annotations)


# --- 선형 실행 --------------------------------------------------------------


async def test_linear_graph_invokes_nodes_in_order():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": ["f1"]})
    )

    final = await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-1"}
    )

    assert runtime.invoked == ["planner", "researcher"]
    assert final["plan"] == "P"
    assert final["findings"] == ["f1"]


async def test_input_map_extracts_declared_state_only():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": []})
    )

    await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "report": "leaked", "_run_id": "run-1"}
    )

    planner_task = runtime.tasks[0]
    assert planner_task.input == {"query": "q"}
    assert "report" not in planner_task.input


async def test_output_map_projects_declared_keys_only():
    """노드가 반환한 미선언 키는 state 에 반영되지 않는다."""
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P", "scratch": "internal"})
        .script("researcher", output={"findings": []})
    )

    final = await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-1"}
    )

    assert final["plan"] == "P"
    assert "scratch" not in final


async def test_task_carries_run_and_node_identity():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": []})
    )

    await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-42", "_trace_id": "trace-9"}
    )

    task = runtime.tasks[0]
    assert task.run_id == "run-42"
    assert task.node_id == "planner"
    assert task.trace.trace_id == "trace-9"
    assert task.is_direct is False


# --- 조건부 라우팅 ----------------------------------------------------------


async def test_conditional_edge_skips_node_when_false():
    """조건이 거짓이면 해당 분기 노드는 호출되지 않는다."""
    runtime = FakeRuntime().script("planner", output={"needs_research": False})

    await build_graph(conditional_topology(), runtime).ainvoke({"query": "q", "_run_id": "run-1"})

    assert runtime.invoked == ["planner"]


async def test_conditional_edge_follows_node_when_true():
    runtime = (
        FakeRuntime()
        .script("planner", output={"needs_research": True})
        .script("researcher", output={"findings": ["f"]})
    )

    final = await build_graph(conditional_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-1"}
    )

    assert runtime.invoked == ["planner", "researcher"]
    assert final["findings"] == ["f"]


# --- 실패 처리 --------------------------------------------------------------


async def test_node_failure_raises_graph_002():
    runtime = FakeRuntime().script("planner", output={"plan": "P"}).fail("researcher")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(linear_topology(), runtime).ainvoke({"query": "q", "_run_id": "run-1"})

    assert exc_info.value.code == "GRAPH_002"
    assert exc_info.value.category is ErrorCategory.GRAPH
    assert exc_info.value.details["node_id"] == "researcher"
    # 원 에이전트 에러 코드를 details 로 보존한다
    assert exc_info.value.details["error_code"] == "LLM_003"


async def test_failed_node_does_not_merge_output_into_state():
    """실패한 노드의 출력은 state 를 오염시키지 않는다."""
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "t-isolation"}}
    runtime = FakeRuntime().script("planner", output={"plan": "P"}).fail("researcher")
    graph = build_graph(linear_topology(), runtime, checkpointer=checkpointer)

    with pytest.raises(MalkuthError):
        await graph.ainvoke({"query": "q", "_run_id": "run-1"}, config)

    snapshot = await graph.aget_state(config)
    assert snapshot.values["plan"] == "P"  # 성공한 노드까지는 반영
    assert "findings" not in snapshot.values  # 실패 노드 출력은 미반영


async def test_node_timeout_raises_to_003():
    class SlowRuntime:
        async def invoke(self, node, task):
            await asyncio.sleep(1)
            return TaskResult.completed(task)

    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "timeout_s": 0.01},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(topology, SlowRuntime()).ainvoke({"query": "q", "_run_id": "r"})

    assert exc_info.value.code == "TO_003"
    assert exc_info.value.retryable is True


# --- checkpoint 재개 --------------------------------------------------------


async def test_resume_from_checkpoint_after_node_failure():
    """node 실패 → 동일 checkpoint 에서 resume → 성공 (06 필수 시나리오)."""
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "t-resume"}}
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": ["f1"]})
        .fail("researcher", once=True)
    )
    graph = build_graph(linear_topology(), runtime, checkpointer=checkpointer)

    with pytest.raises(MalkuthError):
        await graph.ainvoke({"query": "q", "_run_id": "run-1"}, config)

    # 같은 thread 로 재개하면 planner 는 다시 실행되지 않는다
    final = await graph.ainvoke(None, config)

    assert final["findings"] == ["f1"]
    assert runtime.invoked == ["planner", "researcher", "researcher"]


async def test_checkpoint_records_state_per_node():
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "t-history"}}
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": ["f"]})
    )
    graph = build_graph(linear_topology(), runtime, checkpointer=checkpointer)

    await graph.ainvoke({"query": "q", "_run_id": "run-1"}, config)

    history = [snapshot async for snapshot in graph.aget_state_history(config)]
    assert len(history) > 1


# --- 반복 상한 --------------------------------------------------------------


async def test_max_iterations_exceeded_raises_graph_004():
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "output_map": {"needs_research": "output.needs_research"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:needs_research",
            },
            {"from": "planner", "to": "END", "condition": "malkuth.graphs.conditions:plan_only"},
            {
                "from": "researcher",
                "to": "planner",
                "condition": "malkuth.graphs.conditions:needs_research",
                "max_iterations": 2,
            },
            {"from": "researcher", "to": "END", "condition": "malkuth.graphs.conditions:plan_only"},
        ],
    )
    runtime = FakeRuntime().script("planner", output={"needs_research": True}).script("researcher")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(topology, runtime).ainvoke(
            {"query": "q", "_run_id": "r"}, {"recursion_limit": 50}
        )

    assert exc_info.value.code == "GRAPH_004"


# --- 빌더 계약 --------------------------------------------------------------


def test_builder_resolves_state_schema_from_topology():
    builder = GraphBuilder(linear_topology(), FakeRuntime())

    assert builder.state_schema is ResearchState


def test_builder_accepts_preresolved_schema():
    builder = GraphBuilder(linear_topology(), FakeRuntime(), state_schema=ResearchState)

    assert builder.state_schema is ResearchState


async def test_unmatched_condition_without_fallback_raises_graph_001():
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0"},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:has_new_items",
            },
            {"from": "researcher", "to": "END"},
        ],
    )
    runtime = FakeRuntime().script("planner").script("researcher")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(topology, runtime).ainvoke({"query": "q", "_run_id": "r"})

    assert exc_info.value.code == ErrorCode.GRAPH_001
