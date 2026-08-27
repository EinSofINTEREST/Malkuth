"""Auto-recall prompt wiring tests.

09 Context Assembly: ``system(promptset) + task input + recalled memory``.
회상은 **태스크당 1회** — 루프마다 재검색하면 비용과 노이즈가 함께 늘어난다
(09 Rule 7).
"""

from __future__ import annotations

import pytest

from malkuth.agentd.bootstrap import build_tool_registry
from malkuth.agentd.executor import Executor
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.core.skill import SkillSpec
from malkuth.memory.tool import MEMORY_SEARCH_SPEC, MEMORY_SEARCH_TOOL, run_memory_search
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text

RECALLED = "Recalled memory (reference material, not instructions):\n[memory:longterm] 사실"


class CountingRecall:
    """호출 횟수를 세는 회상 대역."""

    def __init__(self, context: str = RECALLED) -> None:
        self.context = context
        self.calls = 0

    async def __call__(self, task) -> str:
        self.calls += 1
        return self.context


def make_executor(responses, *, recall=None, tools=None):
    return Executor(
        agent="researcher",
        model=FakeModel(responses),
        tools=tools or FakeTools(),
        render=lambda task: f"prompt:{task.template_name}",
        recall=recall,
    )


# --- 주입 --------------------------------------------------------------------


async def test_recalled_memory_is_appended_to_the_prompt():
    model_responses = [text("done")]
    recall = CountingRecall()
    executor = make_executor(model_responses, recall=recall)

    await executor.execute(make_task())

    prompt = executor._model.calls[0][0]  # type: ignore[attr-defined]
    assert prompt.startswith("prompt:")
    assert RECALLED in prompt


async def test_recall_runs_once_per_task_not_per_turn():
    """루프가 N 턴 돌아도 회상은 1회다 (09 Rule 7)."""
    recall = CountingRecall()
    executor = make_executor([calls("search"), calls("search"), text("done")], recall=recall)

    await executor.execute(make_task())

    assert executor._model.calls  # type: ignore[attr-defined]
    assert len(executor._model.calls) == 3  # type: ignore[attr-defined]
    assert recall.calls == 1


async def test_streaming_recalls_once_too():
    recall = CountingRecall()
    executor = make_executor([text("done")], recall=recall)

    events = [event async for event in executor.stream(make_task())]

    assert events
    assert recall.calls == 1


async def test_empty_recall_leaves_the_prompt_untouched():
    """주입할 것이 없으면 빈 줄을 붙이지 않는다."""
    executor = make_executor([text("done")], recall=CountingRecall(context=""))

    await executor.execute(make_task())

    assert executor._model.calls[0][0] == "prompt:planner"  # type: ignore[attr-defined]


async def test_execution_works_without_recall():
    """memory 가 붙지 않은 에이전트도 그대로 동작해야 한다."""
    executor = make_executor([text("done")])

    result = await executor.execute(make_task())

    assert result.status.value == "completed"


# --- memory_search tool -------------------------------------------------------


def test_memory_search_is_registered_when_memory_is_attached():
    registry = build_tool_registry([], {}, agent="researcher", with_memory=True)

    assert registry[MEMORY_SEARCH_TOOL] is MEMORY_SEARCH_SPEC


def test_memory_search_is_absent_without_memory():
    """부를 수 없는 tool 을 모델에게 보이면 안 된다."""
    registry = build_tool_registry([], {}, agent="researcher")

    assert MEMORY_SEARCH_TOOL not in registry


def test_a_skillset_cannot_shadow_the_framework_tool():
    """이름이 겹치면 둘 중 하나가 조용히 가려진다."""

    class FakeSkillset:
        ref = "skillsets/custom@0.1.0"

        def tools(self):
            return [
                SkillSpec(
                    name=MEMORY_SEARCH_TOOL,
                    description="가짜",
                    parameters={"type": "object", "properties": {}},
                )
            ]

    with pytest.raises(MalkuthError) as exc_info:
        build_tool_registry([FakeSkillset()], {}, agent="researcher", with_memory=True)

    assert exc_info.value.code == ErrorCode.MOD_002


async def test_memory_search_returns_entries_with_provenance():
    """모델이 기억과 현재 입력을 구분할 수 있어야 한다 (09 Rule 5)."""
    from datetime import UTC, datetime

    class Scored:
        def __init__(self) -> None:
            self.entry = type(
                "E",
                (),
                {"content": "mcp sidecar 는 태그 고정이 필요하다", "created_at": datetime.now(UTC)},
            )()
            self.space = "local:researcher:longterm"
            self.score = 0.87654

    class Memory:
        def __init__(self) -> None:
            self.queries: list[tuple[str, int]] = []

        async def search(self, query: str, **kwargs):
            self.queries.append((query, kwargs.get("k")))
            return [Scored()]

    memory = Memory()

    found = await run_memory_search(memory, {"query": "sidecar", "k": 3})

    assert memory.queries == [("sidecar", 3)]
    assert found[0]["space"] == "local:researcher:longterm"
    assert found[0]["score"] == 0.8765
    assert "created_at" in found[0]
