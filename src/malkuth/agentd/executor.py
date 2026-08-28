"""Model call and tool execution loop.

agentd 의 실행 루프. 모델과 tool 사이를 오가며 태스크를 완수한다.

모델 provider 의 자체 재시도는 비활성화하고 이 계층이 단일 재시도 계층이 된다
(05 Retry Layering) — 두 계층이 겹치면 backoff 가 곱해진다.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from malkuth.agentd.telemetry import STATUS_COMPLETED, STATUS_FAILED, STATUS_RATE_LIMITED
from malkuth.core.agent import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TOOL_TIMEOUT_S,
    ModelUsage,
    TaskRequest,
    TaskResult,
)
from malkuth.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    ErrorCategory,
    ErrorCode,
    MalkuthError,
)
from malkuth.core.events import (
    DoneEvent,
    ErrorEvent,
    TaskEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from malkuth.core.skill import SkillContext
from malkuth.core.tools import is_mcp_tool
from malkuth.resilience import retrying_any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from malkuth.agentd.telemetry import ExecutorTelemetry
    from malkuth.core.errors import RetryPolicy
    from malkuth.core.skill import ArtifactStore

    TaskRecall = Callable[[TaskRequest], Awaitable[str]]
    """태스크 진입 시 1회 회상해 프롬프트에 붙일 텍스트를 만드는 콜러블."""


MODEL_RETRY_POLICIES: Final = (RATE_LIMIT_RETRY, NETWORK_RETRY)
"""모델 호출이 내는 두 실패에 각각의 backoff — rate limit 을 1초 간격으로
두드리면 상황이 악화된다. 순서가 곧 우선순위다.

**기본값이 아니다**: 재시도는 실제로 기다리므로, 조립하는 쪽(`build_executor`)
이 명시적으로 켠다. 기본으로 켜면 fake model 로 실패를 스크립트하는 모든
테스트가 초 단위로 기다리게 된다 (06 Async 2).
"""


def _model_status(err: BaseException) -> str:
    """실패를 05 의 status 어휘로 분류한다.

    rate limit 을 failed 로 뭉개면 ModelRateLimited 알림이 영원히 침묵한다 —
    05 status 표가 명시적으로 경고하는 상황이다.
    """
    if isinstance(err, MalkuthError) and err.category is ErrorCategory.RATE_LIMIT:
        return STATUS_RATE_LIMITED
    return STATUS_FAILED


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    모델이 요청한 tool 호출.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    """One model turn.

    모델의 한 턴. tool 호출이 없으면 태스크가 끝난 것으로 본다.
    """

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)

    @property
    def is_final(self) -> bool:
        """더 이상 tool 을 부르지 않는 최종 응답인지."""
        return not self.tool_calls


class Model(Protocol):
    """Model provider contract.

    모델 provider 계약. 실제 SDK 는 이 뒤에 감춰지고, 테스트는 FakeModel 로 대체한다.
    """

    async def run(self, prompt: str, tools: Sequence[Any]) -> ModelResponse:
        """프롬프트와 tool 목록으로 한 턴을 실행한다."""
        ...


class ToolRegistry(Protocol):
    """Resolves a tool name to its callable.

    tool 이름을 실행 가능한 함수로 해석하는 계약. skillset tool 과 MCP tool 을
    한 곳에서 조회하되, 실패 시 출처에 맞는 에러 코드로 변환할 수 있어야 한다.
    """

    def timeout_for(self, name: str) -> float:
        """해당 tool 의 실행 상한."""
        ...

    async def call(self, name: str, arguments: Mapping[str, Any], ctx: SkillContext) -> Any:
        """tool 을 실행한다."""
        ...


@dataclass
class ExecutorConfig:
    """Execution limits for the loop.

    루프 실행 상한. 태스크별 값이 있으면 그쪽이 우선한다.
    """

    max_turns: int = DEFAULT_MAX_TURNS
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S


def _tool_error(name: str, task: TaskRequest, agent: str, err: BaseException) -> MalkuthError:
    """tool 실패를 출처에 맞는 코드로 변환한다.

    skillset tool 은 ``SKILL_001``, MCP tool 은 ``MCP_003`` — 출처가 다르면
    재시도·알림 전략도 다르기 때문이다 (05 Layer Rules).
    """
    if isinstance(err, MalkuthError):
        return err

    is_mcp = is_mcp_tool(name)
    return MalkuthError(
        category=ErrorCategory.MCP if is_mcp else ErrorCategory.MODULE,
        code=ErrorCode.MCP_003 if is_mcp else ErrorCode.SKILL_001,
        message=f"tool call failed: {name}",
        agent=agent,
        task_id=task.task_id,
        details={"tool": name},
    )


class Executor:
    """Runs a task through the model/tool loop.

    태스크를 모델·tool 루프로 실행한다.
    """

    def __init__(
        self,
        *,
        agent: str,
        model: Model,
        tools: ToolRegistry,
        render: Callable[[TaskRequest], str],
        tool_schemas: Sequence[Any] = (),
        config: ExecutorConfig | None = None,
        on_cleanup: Callable[[], None] | None = None,
        telemetry: ExecutorTelemetry | None = None,
        recall: TaskRecall | None = None,
        artifacts: ArtifactStore | None = None,
        output_keys: Callable[[TaskRequest], Sequence[str]] | None = None,
        retry_policies: Sequence[RetryPolicy] = (),
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._agent = agent
        # 미주입 시 skill 은 ctx.artifacts is None 을 받는다 — 지금까지 **항상**
        # 그랬고, 그래서 02 Output Discipline 의 참조 전달 경로가 죽어 있었다
        self._artifacts = artifacts
        # 키는 **태스크마다** 다르다: 같은 에이전트가 노드마다 다른 템플릿을
        # 쓰고, 계약은 그 템플릿에 붙어 있다 (#150)
        self._output_keys = output_keys
        self._recall = recall
        self._telemetry = telemetry
        self._model = model
        self._tools = tools
        self._render = render
        self._tool_schemas = list(tool_schemas)
        self._config = config or ExecutorConfig()
        self._on_cleanup = on_cleanup
        # 05 Retry Layering — 모델 호출의 재시도 주체는 agentd 다.
        # 비어 있으면 재시도하지 않는다 — 조립하는 쪽이 켠다
        self._retry_policies = tuple(retry_policies)
        # 06 은 시간 의존 로직이 테스트에서 실제로 자는 것을 금지한다
        self._retry_sleep = retry_sleep
        # 멱등성: 완료된 태스크는 같은 결과를 돌려준다 (재시도/재개 시나리오)
        self._completed: dict[str, TaskResult] = {}

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Run a task to completion.

        태스크를 완수합니다. 실패는 예외가 아니라 ``TaskResult`` 로 보고하므로
        데몬이 죽지 않습니다 — 단 취소는 그대로 전파됩니다.

        Args:
            task: The task to run.

        Returns:
            The task result, successful or failed.
        """
        cached = self._completed.get(task.task_id)
        if cached is not None:
            return cached

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._run(task), timeout=task.config.timeout_s)
        except TimeoutError:
            result = TaskResult.failed(
                task,
                MalkuthError(
                    category=ErrorCategory.TIMEOUT,
                    code=ErrorCode.TO_001,
                    message="task timeout exceeded",
                    agent=self._agent,
                    task_id=task.task_id,
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            # 취소는 협조적 종료다 — 결과로 삼키지 않고 전파한다
            self._cleanup()
            raise
        except MalkuthError as err:
            result = TaskResult.failed(task, err)
        except Exception as err:
            # 모델 provider 예외가 새어나가면 데몬이 죽는다 — tool 쪽은 이미
            # _tool_error 가 변환하는데 모델 쪽만 빠져 있었다
            result = TaskResult.failed(
                task,
                MalkuthError(
                    category=ErrorCategory.INTERNAL,
                    code=ErrorCode.INTERNAL_001,
                    message="unexpected error during task execution",
                    agent=self._agent,
                    task_id=task.task_id,
                    details={"cause": type(err).__name__},
                ),
            )

        self._record_task(result, task=task, duration_s=time.perf_counter() - started)

        # 재시도 가능한 실패를 캐싱하면 이 계층이 유일한 재시도 계층인데도
        # 재시도가 영원히 무효화된다 — 성공과 영구 실패만 기억한다
        if result.error is None or not result.error.retryable:
            self._completed[task.task_id] = result
        return result

    def _record_task(self, result: TaskResult, *, task: TaskRequest, duration_s: float) -> None:
        """태스크 종료를 메트릭에 남긴다 — telemetry 미주입 시 무동작."""
        if self._telemetry is None:
            return
        self._telemetry.task_finished(
            status=result.status.value, duration_s=duration_s, graph=task.trace.graph
        )

    def _shape_output(self, content: str, task: TaskRequest) -> dict[str, Any]:
        """Build the task output from the model's final response.

        선언된 키가 없으면 기존대로 ``{"content": ...}`` 하나입니다.

        선언이 있으면 응답을 JSON 으로 읽어 그 키들만 옮깁니다 — 여분 키는
        버립니다 (선언되지 않은 값이 state 로 흘러가면 02 Rule 5 의 출력
        규율이 깨집니다).

        Raises:
            MalkuthError: MODEL/``LLM_004`` if the response is not JSON or a
                declared key is missing — 조용히 빈 출력으로 떨어지면 그래프가
                다음 노드에서야 GRAPH_003 으로 실패해 원인이 멀어집니다.
        """
        keys = tuple(self._output_keys(task)) if self._output_keys else ()
        if not keys:
            return {"content": content}

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as err:
            raise self._output_error("model response is not json", content, keys) from err

        if not isinstance(parsed, dict):
            raise self._output_error("model response is not a json object", content, keys)

        missing = [key for key in keys if key not in parsed]
        if missing:
            raise self._output_error(f"declared output keys missing: {missing}", content, keys)

        return {key: parsed[key] for key in keys}

    def _output_error(self, message: str, content: str, keys: Sequence[str]) -> MalkuthError:
        """출력 계약 위반 — 응답 꼬리를 남겨 promptset 드리프트를 추적한다."""
        return MalkuthError(
            category=ErrorCategory.MODEL,
            code=ErrorCode.LLM_004,
            message=message,
            agent=self._agent,
            details={"declared": ",".join(keys), "content": content[-300:]},
        )

    async def _initial_prompt(self, task: TaskRequest) -> str:
        """Build the task-entry prompt, recalling memory once.

        09 Context Assembly 의 구성 순서를 따릅니다:
        ``system(promptset) + task input + recalled memory``.

        회상은 **태스크당 1회**입니다 — tool loop 가 N 턴 돌아도 다시 검색하지
        않습니다 (09 Rule 7). 추가 탐색은 모델이 ``memory_search`` 를 명시
        호출합니다.
        """
        prompt = self._render(task)
        if self._recall is None:
            return prompt

        context = await self._recall(task)
        if not context:
            return prompt
        return f"{prompt}\n\n{context}"

    async def _run(self, task: TaskRequest) -> TaskResult:
        """모델과 tool 을 오가며 루프를 돈다."""
        prompt = await self._initial_prompt(task)
        usage = ModelUsage()
        # 태스크가 기본값을 그대로 쓰면 executor 설정을 따른다 —
        # `or` 로 고르면 TaskConfig 의 기본값(20)이 항상 이겨 설정이 무시된다
        max_turns = min(task.config.max_turns, self._config.max_turns)

        try:
            for turn in range(max_turns):
                response = await self._model_turn(prompt)
                usage = usage.merge(response.usage)

                if response.is_final:
                    return TaskResult.completed(
                        task, output=self._shape_output(response.content, task), usage=usage
                    )

                results = await self._run_tools(response.tool_calls, task, turn)
                prompt = self._extend(prompt, response, results)
        except asyncio.CancelledError:
            self._cleanup()
            raise

        raise MalkuthError(
            category=ErrorCategory.MODEL,
            code=ErrorCode.LLM_005,
            message="max turns exceeded",
            agent=self._agent,
            task_id=task.task_id,
            details={"max_turns": max_turns},
        )

    async def _call_model(self, prompt: str) -> ModelResponse:
        """모델 한 번 — 실패도 메트릭에 남겨야 하므로 여기서 감싼다.

        **재시도 안쪽**이라 시도마다 계수된다: 05 의 rate limit 알림은
        발생 빈도를 보므로, 재시도로 성공한 호출의 rate limit 이 지워지면
        provider 압박이 지표에서 사라진다.
        """
        if self._telemetry is None:
            return await self._model.run(prompt, self._tool_schemas)

        try:
            response = await self._model.run(prompt, self._tool_schemas)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._telemetry.model_called(status=_model_status(err))
            raise

        self._telemetry.model_called(status=STATUS_COMPLETED, usage=response.usage)
        return response

    async def _model_turn(self, prompt: str) -> ModelResponse:
        """모델 한 턴 — 정책이 허용하는 실패는 재시도한다.

        **재시도 주체는 agentd 다** (05 Retry Layering). provider SDK 재시도는
        어댑터가 꺼 두었으므로 backoff 가 곱해지지 않는다.

        rate limit 은 다른 정책을 쓴다 — 10초에서 시작해 5회. 네트워크 실패의
        1초 backoff 로 rate limit 을 두드리면 상황을 악화시킨다.
        """

        async def attempt() -> ModelResponse:
            return await self._call_model(prompt)

        return await retrying_any(
            self._retry_policies, attempt, sleep=self._retry_sleep, agent=self._agent
        )

    async def _run_tools(
        self, calls: Sequence[ToolCall], task: TaskRequest, turn: int
    ) -> list[tuple[ToolCall, Any]]:
        """독립 tool 호출을 병렬 실행한다.

        하나가 실패해도 형제 호출을 중도 취소하지 않는다 — 취소된 호출은
        결과도 메트릭도 남기지 못해, 다중 tool 턴의 집계가 조용히 비게 된다.
        """
        ctx = SkillContext(
            agent=self._agent,
            task_id=task.task_id,
            run_id=task.run_id,
            artifacts=self._artifacts,
        )
        coros = [self._run_tool(call, task, ctx) for call in calls]

        try:
            outcomes = await asyncio.gather(*coros, return_exceptions=True)
        except asyncio.CancelledError:
            self._cleanup()
            raise

        for outcome in outcomes:
            if isinstance(outcome, asyncio.CancelledError):
                self._cleanup()
                raise outcome
            if isinstance(outcome, BaseException):
                raise outcome

        return list(zip(calls, outcomes, strict=True))

    def _tool_timeout(self, name: str, task: TaskRequest) -> float:
        """tool 실행 상한 — per-tool 선언이 있으면 그것, 없으면 더 엄격한 쪽."""
        declared = self._tools.timeout_for(name)
        if declared:
            return declared
        return min(task.config.tool_timeout_s, self._config.tool_timeout_s)

    async def _run_tool(self, call: ToolCall, task: TaskRequest, ctx: SkillContext) -> Any:
        """단일 tool 을 상한 안에서 실행하고 실패를 변환한다."""
        timeout = self._tool_timeout(call.name, task)
        try:
            result = await asyncio.wait_for(
                self._tools.call(call.name, call.arguments, ctx), timeout=timeout
            )
        except TimeoutError as err:
            self._record_tool(call.name, status=STATUS_FAILED)
            raise MalkuthError(
                category=ErrorCategory.TIMEOUT,
                code=ErrorCode.TO_002,
                message=f"tool timeout: {call.name}",
                agent=self._agent,
                task_id=task.task_id,
                retryable=True,
                details={"tool": call.name},
            ) from err
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._record_tool(call.name, status=STATUS_FAILED)
            raise _tool_error(call.name, task, self._agent, err) from err

        self._record_tool(call.name, status=STATUS_COMPLETED)
        return result

    def _record_tool(self, tool: str, *, status: str) -> None:
        """tool 호출을 메트릭에 남긴다 — telemetry 미주입 시 무동작."""
        if self._telemetry is not None:
            self._telemetry.tool_called(tool=tool, status=status)

    def _extend(
        self, prompt: str, response: ModelResponse, results: Sequence[tuple[ToolCall, Any]]
    ) -> str:
        """다음 턴의 프롬프트에 이번 턴의 응답과 tool 결과를 잇는다."""
        parts = [prompt]
        if response.content:
            parts.append(response.content)
        parts.extend(f"[tool:{call.name}] {result}" for call, result in results)
        return "\n".join(parts)

    def _cleanup(self) -> None:
        """취소 시 진행 중 자원을 정리한다."""
        if self._on_cleanup is not None:
            self._on_cleanup()

    async def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """Run a task, emitting events as it progresses.

        태스크를 실행하며 진행 이벤트를 발행합니다 — 장시간 태스크의 소비 경로입니다.

        ``execute`` 와 동일한 상한을 적용합니다: 태스크 timeout 을 넘기면
        ``TO_001`` 이벤트로 끝냅니다. 스트리밍은 장시간 태스크의 경로라
        멈춘 provider 를 만날 가능성이 가장 높습니다.

        Args:
            task: The task to run.

        Yields:
            Token, tool call/result, and terminal done/error events.
        """
        deadline = asyncio.get_running_loop().time() + task.config.timeout_s
        events = self._stream(task)

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                yield self._timeout_event(task)
                return
            try:
                event = await asyncio.wait_for(anext(events), timeout=remaining)
            except StopAsyncIteration:
                return
            except TimeoutError:
                yield self._timeout_event(task)
                return
            yield event

    def _recall_failure(self, err: BaseException) -> MalkuthError:
        """회상 실패를 구조화 에러로 만든다 — 기억이 없다고 태스크가 죽지는 않는다."""
        if isinstance(err, MalkuthError):
            return err
        return MalkuthError(
            category=ErrorCategory.MEMORY,
            code=ErrorCode.MEM_004,
            message="auto-recall failed",
            agent=self._agent,
            details={"cause": type(err).__name__},
        )

    def _timeout_event(self, task: TaskRequest) -> ErrorEvent:
        """태스크 상한 초과를 종료 이벤트로 만든다."""
        return ErrorEvent(
            task_id=task.task_id,
            error=MalkuthError(
                category=ErrorCategory.TIMEOUT,
                code=ErrorCode.TO_001,
                message="task timeout exceeded",
                agent=self._agent,
                task_id=task.task_id,
                retryable=True,
            ).payload(),
        )

    async def _stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """이벤트를 발행하며 루프를 돈다 (상한은 stream 이 적용)."""
        try:
            prompt = await self._initial_prompt(task)
        except asyncio.CancelledError:
            self._cleanup()
            raise
        except Exception as err:
            # 같은 실패가 execute 에서는 TaskResult 인데 stream 에서만 예외로
            # 새어나가면 소비자가 두 경로를 다르게 다뤄야 한다
            yield ErrorEvent(task_id=task.task_id, error=self._recall_failure(err).payload())
            return
        usage = ModelUsage()
        # 태스크가 기본값을 그대로 쓰면 executor 설정을 따른다 —
        # `or` 로 고르면 TaskConfig 의 기본값(20)이 항상 이겨 설정이 무시된다
        max_turns = min(task.config.max_turns, self._config.max_turns)
        ctx = SkillContext(
            agent=self._agent,
            task_id=task.task_id,
            run_id=task.run_id,
            artifacts=self._artifacts,
        )

        try:
            for turn in range(max_turns):
                response = await self._model_turn(prompt)
                usage = usage.merge(response.usage)

                if response.content:
                    yield TokenEvent(task_id=task.task_id, text=response.content)

                if response.is_final:
                    yield DoneEvent(
                        task_id=task.task_id,
                        output={"content": response.content},
                        usage=usage,
                    )
                    return

                for call in response.tool_calls:
                    yield ToolCallEvent(
                        task_id=task.task_id,
                        tool=call.name,
                        arguments=dict(call.arguments),
                        turn=turn,
                    )

                # execute 와 동일하게 병렬 실행한다 — 순차로 돌면 스트리밍 경로만
                # tool 개수만큼 느려지고 두 경로의 타이밍이 갈린다
                started = time.monotonic()
                outcomes = await asyncio.gather(
                    *(self._run_tool(call, task, ctx) for call in response.tool_calls),
                    return_exceptions=True,
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)

                results = []
                failure: MalkuthError | None = None
                for call, outcome in zip(response.tool_calls, outcomes, strict=True):
                    if isinstance(outcome, BaseException):
                        if isinstance(outcome, asyncio.CancelledError):
                            raise outcome
                        error = (
                            outcome
                            if isinstance(outcome, MalkuthError)
                            else _tool_error(call.name, task, self._agent, outcome)
                        )
                        failure = failure or error
                        yield ToolResultEvent(
                            task_id=task.task_id,
                            tool=call.name,
                            turn=turn,
                            duration_ms=elapsed_ms,
                            error=error.payload(),
                        )
                        continue
                    yield ToolResultEvent(
                        task_id=task.task_id,
                        tool=call.name,
                        result=outcome,
                        turn=turn,
                        duration_ms=elapsed_ms,
                    )
                    results.append((call, outcome))

                if failure is not None:
                    yield ErrorEvent(task_id=task.task_id, error=failure.payload())
                    return

                prompt = self._extend(prompt, response, results)
        except asyncio.CancelledError:
            self._cleanup()
            raise

        yield ErrorEvent(
            task_id=task.task_id,
            error=MalkuthError(
                category=ErrorCategory.MODEL,
                code=ErrorCode.LLM_005,
                message="max turns exceeded",
                agent=self._agent,
                task_id=task.task_id,
                details={"max_turns": max_turns},
            ).payload(),
        )


__all__ = [
    "Executor",
    "ExecutorConfig",
    "Model",
    "ModelResponse",
    "ToolCall",
    "ToolRegistry",
]
