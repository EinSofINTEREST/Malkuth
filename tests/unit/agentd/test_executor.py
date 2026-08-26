"""Unit tests for the agentd tool execution loop.

실제 LLM 을 호출하지 않는다 — 스크립트된 FakeModel 로만 검증한다 (06 규칙).
"""

from __future__ import annotations

import asyncio

import pytest

from malkuth.agentd.executor import Executor, ExecutorConfig
from malkuth.core.agent import TaskConfig, TaskStatus
from malkuth.core.errors import ErrorCategory, MalkuthError
from malkuth.core.events import DoneEvent, ErrorEvent, ToolCallEvent, ToolResultEvent
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text


def make_executor(
    responses, tools: FakeTools | None = None, *, on_cleanup=None, **config
) -> tuple[Executor, FakeModel, FakeTools]:
    """FakeModel/FakeTools 를 물린 executor."""
    model = FakeModel(responses)
    registry = tools or FakeTools()
    executor = Executor(
        agent="researcher",
        model=model,
        tools=registry,
        render=lambda task: f"prompt:{task.template_name}",
        config=ExecutorConfig(**config) if config else None,
        on_cleanup=on_cleanup,
    )
    return executor, model, registry


# --- 기본 루프 --------------------------------------------------------------


async def test_final_response_completes_the_task():
    executor, model, _ = make_executor([text("done")])

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"content": "done"}
    assert model.turns == 1


async def test_tool_call_then_completion():
    tools = FakeTools().script("search", result="found")
    executor, model, _ = make_executor([calls("search"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert tools.calls == ["search"]
    assert model.turns == 2


async def test_tool_results_feed_the_next_prompt():
    tools = FakeTools().script("search", result="found")
    executor, model, _ = make_executor([calls("search"), text("done")], tools)

    await executor.execute(make_task())

    second_prompt = model.calls[1][0]
    assert "[tool:search] found" in second_prompt


# --- 상한과 타임아웃 --------------------------------------------------------


async def test_max_turns_exceeded_raises_llm_005():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search")], tools, max_turns=3)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == "LLM_005"
    assert result.error.details["max_turns"] == 3


async def test_task_timeout_reports_to_001():
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    result = await executor.execute(make_task(config=TaskConfig(timeout_s=0.05)))

    assert result.error is not None
    assert result.error.code == "TO_001"
    assert result.error.retryable is True


async def test_tool_timeout_reports_to_002():
    tools = FakeTools().script("slow", delay=5).timeout("slow", 0.05)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "TO_002"
    assert result.error.details["tool"] == "slow"


# --- 에러 변환 --------------------------------------------------------------


async def test_skillset_tool_failure_becomes_skill_001():
    """skill 도메인 예외는 SKILL_001 로 감싼다 (05 Layer Rules)."""
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "SKILL_001"
    assert result.error.category is ErrorCategory.MODULE


async def test_mcp_tool_failure_becomes_mcp_003():
    """출처가 다르면 재시도·알림 전략도 달라야 한다."""
    tools = FakeTools().fail("mcp__fs__read_file", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("mcp__fs__read_file"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "MCP_003"
    assert result.error.category is ErrorCategory.MCP


async def test_structured_tool_errors_pass_through_unchanged():
    """이미 구조화된 에러는 재변환하지 않는다."""
    original = MalkuthError(category=ErrorCategory.MCP, code="MCP_002", message="tool not found")
    tools = FakeTools().fail("mcp__fs__missing", original)
    executor, _, _ = make_executor([calls("mcp__fs__missing"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "MCP_002"


async def test_task_failure_does_not_raise():
    """태스크 실패는 결과로 보고한다 — 데몬을 죽이지 않는다."""
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.FAILED


# --- 병렬 실행 --------------------------------------------------------------


async def test_independent_tools_run_in_parallel():
    """직렬 실행이면 0.3s 가 걸린다 — 병렬이면 그보다 훨씬 짧다."""
    tools = FakeTools().script("a", delay=0.1).script("b", delay=0.1).script("c", delay=0.1)
    executor, _, _ = make_executor([calls("a", "b", "c"), text("done")], tools)

    started = asyncio.get_running_loop().time()
    await executor.execute(make_task())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert set(tools.calls) == {"a", "b", "c"}


async def test_all_parallel_tools_start_before_any_completes():
    tools = FakeTools().script("a", delay=0.05).script("b", delay=0.05)
    executor, _, _ = make_executor([calls("a", "b"), text("done")], tools)

    await executor.execute(make_task())

    assert len(tools.started) == 2


# --- usage 집계 -------------------------------------------------------------


async def test_usage_accumulates_across_turns():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor(
        [
            calls("search", input_tokens=10, output_tokens=5),
            text("done", input_tokens=7, output_tokens=3),
        ],
        tools,
    )

    result = await executor.execute(make_task())

    assert result.usage.input_tokens == 17
    assert result.usage.output_tokens == 8


# --- cancellation -----------------------------------------------------------


async def test_cancellation_propagates_and_cleans_up():
    """취소 시 진행 중 tool 을 정리한 뒤 전파한다."""
    cleaned = asyncio.Event()
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools, on_cleanup=cleaned.set)

    task = asyncio.create_task(executor.execute(make_task()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_cancellation_is_not_reported_as_a_result():
    """취소를 TaskResult 로 삼키면 협조적 종료가 실패로 오인된다."""
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    task = asyncio.create_task(executor.execute(make_task()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- 멱등성 ----------------------------------------------------------------


async def test_same_task_id_returns_the_cached_result():
    """재시도/재개 시나리오에서 동일 task_id 재호출은 안전해야 한다."""
    executor, model, _ = make_executor([text("done")])
    task = make_task(task_id="task-idem")

    first = await executor.execute(task)
    second = await executor.execute(task)

    assert first == second
    assert model.turns == 1  # 두 번 실행되지 않는다


async def test_distinct_tasks_execute_separately():
    executor, model, _ = make_executor([text("a"), text("b")])

    await executor.execute(make_task(task_id="t1"))
    await executor.execute(make_task(task_id="t2"))

    assert model.turns == 2


# --- direct 태스크 ----------------------------------------------------------


async def test_direct_task_renders_the_default_template():
    """node_id 가 없으면 default 템플릿 (02 Direct Request Rules 2)."""
    executor, model, _ = make_executor([text("done")])

    await executor.execute(make_task(node_id=None, run_id="direct-1"))

    assert model.calls[0][0] == "prompt:default"


async def test_graph_task_renders_the_node_template():
    executor, model, _ = make_executor([text("done")])

    await executor.execute(make_task(node_id="research"))

    assert model.calls[0][0] == "prompt:research"


# --- 스트리밍 --------------------------------------------------------------


async def test_stream_emits_tool_and_done_events():
    tools = FakeTools().script("search", result="found")
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    events = [event async for event in executor.stream(make_task())]

    kinds = [type(e) for e in events]
    assert ToolCallEvent in kinds
    assert ToolResultEvent in kinds
    assert isinstance(events[-1], DoneEvent)


async def test_stream_carries_the_turn_index():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    events = [e async for e in executor.stream(make_task())]

    tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert tool_calls[0].turn == 0


async def test_stream_reports_tool_failure_as_error_event():
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    events = [e async for e in executor.stream(make_task())]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == "SKILL_001"


async def test_stream_reports_max_turns_as_error_event():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search")], tools, max_turns=2)

    events = [e async for e in executor.stream(make_task())]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == "LLM_005"


async def test_stream_cancellation_cleans_up():
    cleaned = asyncio.Event()
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools, on_cleanup=cleaned.set)

    async def consume():
        async for _ in executor.stream(make_task()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_the_stricter_max_turns_wins():
    """태스크와 executor 중 더 엄격한 상한이 적용돼야 한다.

    `or` 로 고르면 TaskConfig 의 기본값(20)이 항상 이겨 executor 설정이 무시된다.
    """
    tools = FakeTools().script("search")
    executor, model, _ = make_executor([calls("search")], tools, max_turns=2)

    result = await executor.execute(make_task(config=TaskConfig(max_turns=20)))

    assert result.error is not None
    assert result.error.details["max_turns"] == 2
    assert model.turns == 2


async def test_task_can_tighten_the_limit_further():
    tools = FakeTools().script("search")
    executor, model, _ = make_executor([calls("search")], tools, max_turns=10)

    result = await executor.execute(make_task(config=TaskConfig(max_turns=3)))

    assert result.error is not None
    assert result.error.details["max_turns"] == 3
    assert model.turns == 3
