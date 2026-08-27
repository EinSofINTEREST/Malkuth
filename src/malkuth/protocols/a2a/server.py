"""A2A server side — inbound peer calls.

에이전트 컨테이너가 노출하는 A2A 수신부. **allowlist 검증이 SDK 경로 위에서
실제로 일어나는 곳**이다: caller 가 자기 이름을 주장하는 것만으로는 부족하므로
runtime 이 발급한 per-edge token 을 함께 검증한다 (03 Enforcement 이중 방어).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from a2a.server.agent_execution import AgentExecutor
from a2a.types import a2a_pb2 as pb

from malkuth.core.agent import TaskConfig, TaskRequest, TaskStatus, TraceContext
from malkuth.protocols.a2a.errors import depth_exceeded, not_allowed
from malkuth.protocols.a2a.sdk import CALLER_HEADER, TOKEN_HEADER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from malkuth.core.agent import TaskResult
    from malkuth.protocols.a2a.client import A2AServer

    InboundHandler = Callable[[TaskRequest], Awaitable[TaskResult]]

_STATE_OF = {
    TaskStatus.COMPLETED: pb.TaskState.TASK_STATE_COMPLETED,
    TaskStatus.FAILED: pb.TaskState.TASK_STATE_FAILED,
    TaskStatus.CANCELED: pb.TaskState.TASK_STATE_CANCELED,
}
"""내부 상태 → SDK enum. ``sdk.STATE_NAMES`` 의 역방향이다."""

DEFAULT_MAX_DEPTH = 3
"""위임 체인 깊이 상한 — 초과 시 ``A2A_005`` (03 Rule 5)."""

log = structlog.get_logger(__name__)


def headers_of(context: Any) -> Mapping[str, str]:
    """수신 컨텍스트에서 헤더를 꺼낸다 — SDK 가 ``state`` 에 실어 준다."""
    state = getattr(context, "state", None) or {}
    headers = state.get("headers") or {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def read_task(text: str) -> TaskRequest:
    """peer 가 보낸 본문을 ``TaskRequest`` 로 되돌린다.

    본문은 **untrusted input** 이다 — 계약에 맞지 않으면 그대로 거절한다.
    """
    payload = json.loads(text)
    trace = TraceContext.model_validate(payload.get("trace") or {"trace_id": "unknown"})
    return TaskRequest(
        task_id=str(payload["task_id"]),
        run_id=str(payload["run_id"]),
        node_id=payload.get("node_id"),
        input=dict(payload.get("input") or {}),
        config=TaskConfig(),
        trace=trace,
    )


@dataclass
class InboundGuard:
    """Verifies inbound peer calls before they reach the executor.

    수신 호출을 실행 앞단에서 검증합니다. 검증을 통과하지 못한 호출은
    **에이전트 코드에 닿지 않습니다**.
    """

    server: A2AServer
    max_depth: int = DEFAULT_MAX_DEPTH

    def check(self, headers: Mapping[str, str], task: TaskRequest) -> str:
        """Authorize one inbound call.

        수신 호출 하나를 검증합니다.

        Args:
            headers: Inbound headers — caller 이름과 per-edge token 을 담습니다.
            task: The decoded task — its trace carries the delegation depth.

        Returns:
            The verified caller name.

        Raises:
            MalkuthError: A2A/``A2A_004`` if the caller is undeclared or the
                token does not match, ``A2A_005`` if the chain is too deep.
        """
        caller = headers.get(CALLER_HEADER, "")
        token = headers.get(TOKEN_HEADER, "")
        if not caller or not token:
            # 헤더가 없으면 방향을 확인할 방법이 없다 — 이름만 주장하는 호출과 같다
            raise not_allowed(caller or "unknown", self.server.agent)

        self.server.authorize(caller, token)

        # 깊이 검사는 **수신 측에서도** 한다: caller 가 자기 depth 를 정직하게
        # 실었다고 믿으면 순환 위임을 caller 하나가 뚫을 수 있다
        if task.trace.depth > self.max_depth:
            raise depth_exceeded(
                caller, self.server.agent, depth=task.trace.depth, limit=self.max_depth
            )

        log.debug(
            "a2a inbound authorized",
            a2a_caller=caller,
            a2a_callee=self.server.agent,
            a2a_task_id=task.task_id,
        )
        return caller


class GuardedExecutor(AgentExecutor):
    """The SDK-side executor that runs one inbound peer task.

    SDK 경로 위에 검증을 얹습니다 — allowlist·token·depth 를 통과하지 못한
    호출은 에이전트 코드에 닿지 않습니다.
    """

    def __init__(self, guard: InboundGuard, invoke: InboundHandler) -> None:
        self._guard = guard
        self._invoke = invoke

    async def execute(self, context: Any, event_queue: Any) -> None:
        """수신 태스크 하나를 검증하고 실행한다."""
        task = read_task(context.get_user_input())
        self._guard.check(headers_of(context.call_context), task)

        result = await self._invoke(task)
        await event_queue.enqueue_event(
            pb.Task(
                # id 는 **서버가 정한 것**을 쓴다 — 우리 내부 id 로 답하면
                # TaskManager 가 다른 task 의 이벤트로 보고 거절한다
                id=context.task_id,
                context_id=context.context_id,
                status=pb.TaskStatus(state=_STATE_OF[result.status]),
                artifacts=[pb.Artifact(parts=[pb.Part(text=encode_result(result))])],
            )
        )

    async def cancel(self, context: Any, event_queue: Any) -> None:
        """취소는 상위 계층이 관리한다 — 여기서는 no-op."""


def encode_result(result: TaskResult) -> str:
    """결과를 peer 가 읽을 본문으로 — ``read_output`` 의 짝."""
    return json.dumps(result.output, ensure_ascii=False)


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "GuardedExecutor",
    "InboundGuard",
    "encode_result",
    "headers_of",
    "read_task",
]
