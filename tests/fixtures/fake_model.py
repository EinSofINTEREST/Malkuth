"""Scripted model and tool registry doubles.

테스트는 실제 LLM 을 호출하지 않는다 (06 규칙) — 스크립트된 응답을 순서대로
돌려주는 대역을 쓴다. CI 에 provider API key 가 없어야 정상이다.
"""

from __future__ import annotations

import asyncio
from typing import Any

from malkuth.agentd.executor import ModelResponse, ToolCall
from malkuth.core.agent import ModelUsage
from malkuth.core.skill import SkillContext


class FakeModel:
    """스크립트된 응답을 순서대로 반환하는 모델 대역."""

    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def run(self, prompt: str, tools: Any) -> ModelResponse:
        self.calls.append((prompt, tuple(tools)))
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        item = self._responses[index]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def turns(self) -> int:
        return len(self.calls)


class FakeTools:
    """tool 실행을 스크립트하는 registry 대역."""

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}
        self._errors: dict[str, Exception] = {}
        self._delays: dict[str, float] = {}
        self._timeouts: dict[str, float] = {}
        self.calls: list[str] = []
        self.started: list[str] = []

    def script(self, name: str, result: Any = "ok", *, delay: float = 0.0) -> FakeTools:
        self._results[name] = result
        if delay:
            self._delays[name] = delay
        return self

    def fail(self, name: str, error: Exception) -> FakeTools:
        self._errors[name] = error
        return self

    def timeout(self, name: str, seconds: float) -> FakeTools:
        self._timeouts[name] = seconds
        return self

    def timeout_for(self, name: str) -> float:
        return self._timeouts.get(name, 0.0)

    async def call(self, name: str, arguments: Any, ctx: SkillContext) -> Any:
        self.started.append(name)
        delay = self._delays.get(name, 0.0)
        if delay:
            await asyncio.sleep(delay)
        self.calls.append(name)
        error = self._errors.get(name)
        if error is not None:
            raise error
        return self._results.get(name, "ok")


def text(content: str, *, input_tokens: int = 0, output_tokens: int = 0) -> ModelResponse:
    """tool 을 부르지 않는 최종 응답."""
    return ModelResponse(
        content=content,
        usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def calls(*names: str, input_tokens: int = 0, output_tokens: int = 0) -> ModelResponse:
    """주어진 tool 들을 호출하는 응답."""
    return ModelResponse(
        tool_calls=tuple(ToolCall(id=f"c{i}", name=n) for i, n in enumerate(names)),
        usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )
