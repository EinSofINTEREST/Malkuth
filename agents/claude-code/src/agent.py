"""Claude Code as a Malkuth agent.

Claude Code CLI 를 자식 프로세스로 구동하는 실행기. agentd 의 기본 루프는
Messages API 를 직접 호출하지만, 이 실행기는 **Claude Code 자신의 루프**에
태스크를 맡긴다 — tool 사용과 파일 조작은 CLI 안에서 일어난다.

02 Custom Agent 경로(`spec.entrypoint`)로 로드된다.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import TYPE_CHECKING, Any

import structlog

from malkuth.core.agent import TaskResult, TaskStatus
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.events import DoneEvent, TokenEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from malkuth.core.agent import TaskRequest
    from malkuth.core.events import TaskEvent

COMMAND_ENV = "MALKUTH_CLAUDE_COMMAND"
"""CLI 호출 명령 — 플래그가 바뀌어도 이미지 교체로 끝나도록 env 로 받는다."""

DEFAULT_COMMAND = "claude -p --output-format json"
"""기본 호출. 코드에 플래그를 박지 않는다 (02 Manifest Rules 3)."""

PROMPT_KEY = "prompt"
"""태스크 입력에서 프롬프트로 쓰는 키."""

log = structlog.get_logger(__name__)


def resolve_command() -> Sequence[str]:
    """이 컨테이너가 호출할 명령.

    셸 문자열을 그대로 넘기지 않고 토큰으로 쪼갠다 — `shell=True` 는 태스크
    입력이 명령으로 해석될 여지를 만든다 (03 MCP Security 5 와 같은 이유).
    """
    return shlex.split(os.environ.get(COMMAND_ENV) or DEFAULT_COMMAND)


def build_prompt(task: TaskRequest) -> str:
    """태스크 입력을 프롬프트로 옮긴다.

    ``prompt`` 키가 있으면 그것을, 없으면 입력 전체를 JSON 으로 넘긴다 —
    조용히 빈 프롬프트를 보내면 모델이 맥락 없이 답한다.
    """
    value = task.input.get(PROMPT_KEY)
    if isinstance(value, str) and value.strip():
        return value
    return json.dumps(task.input, ensure_ascii=False)


def read_result(raw: str) -> dict[str, Any]:
    """CLI 출력에서 산출물을 꺼낸다.

    JSON 이 아니면 원문을 그대로 싣는다 — 파싱 실패로 결과를 버리면 태스크가
    조용히 빈 채로 끝난다.
    """
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}

    if isinstance(parsed, dict):
        # CLI 의 구조화 출력은 결과를 result 에 담는다 — 없으면 전체를 싣는다
        result = parsed.get("result")
        return {"result": result, "raw": parsed} if result is not None else parsed
    return {"result": parsed}


def cli_error(raw: str) -> str | None:
    """CLI 가 stdout 에 실어 보낸 실패 사유 — 실패가 아니면 ``None``.

    구조화 출력은 실패도 0 이 아닌 종료코드와 **함께 stdout 에 JSON 으로**
    남긴다 (``is_error: true`` + ``result``). 그 사유를 읽지 않으면
    "exited with a failure" 만 남아 원인을 알 수 없다.
    """
    try:
        parsed = json.loads(raw.strip() or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not parsed.get("is_error"):
        return None
    reason = parsed.get("result")
    return str(reason) if reason else "claude code reported a failure"


class ClaudeCodeExecutor:
    """Runs one task through the Claude Code CLI.

    태스크 하나를 Claude Code CLI 로 실행합니다.

    Attributes:
        manifest: 이 에이전트의 선언 — 로깅과 에러 컨텍스트에 쓰입니다.
    """

    def __init__(self, manifest: Any) -> None:
        self.manifest = manifest
        self.agent = manifest.name

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Run the task and return its result.

        태스크를 실행하고 결과를 돌려줍니다. 실패는 예외가 아니라
        ``TaskResult`` 로 보고합니다 — 태스크 실패가 데몬을 죽이면 안 됩니다
        (02 Rule 4).

        Args:
            task: The task to run.

        Returns:
            The completed or failed result.
        """
        try:
            output = await self._run(task)
        except MalkuthError as err:
            log.error(
                "claude code task failed",
                agent=self.agent,
                task_id=task.task_id,
                error_code=str(err.code),
            )
            return TaskResult.failed(task, err.payload())

        return TaskResult.completed(task, output=output)

    async def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """Stream the task result.

        CLI 를 한 번에 실행하므로 결과를 한 이벤트로 흘린 뒤 완료를 알립니다 —
        토큰 단위 스트리밍은 CLI 의 스트림 출력을 붙일 때 열립니다.
        """
        result = await self.execute(task)
        if result.status is TaskStatus.COMPLETED:
            yield TokenEvent(task_id=task.task_id, text=json.dumps(result.output))
        yield DoneEvent(task_id=task.task_id, status=result.status, output=result.output)

    async def _run(self, task: TaskRequest) -> dict[str, Any]:
        """CLI 를 자식 프로세스로 실행한다."""
        command = [*resolve_command(), build_prompt(task)]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=task.config.timeout_s
            )
        except TimeoutError as err:
            # 자식을 정리하지 않으면 컨테이너에 좀비가 쌓인다 (02 Rule 1)
            await self._terminate(process)
            raise self._error(
                ErrorCode.TO_001,
                ErrorCategory.TIMEOUT,
                f"claude code exceeded {task.config.timeout_s}s",
                task,
                retryable=True,
            ) from err
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

        raw = stdout.decode(errors="replace")
        reported = cli_error(raw)

        if process.returncode != 0 or reported is not None:
            # CLI 는 실패 사유를 **stdout 의 JSON** 에 담는다 — stderr 만 보면
            # 운영자가 받는 details 가 비어 원인을 잃는다 (실제로 그랬다)
            raise self._error(
                ErrorCode.LLM_003,
                ErrorCategory.MODEL,
                reported or "claude code exited with a failure",
                task,
                returncode=str(process.returncode),
                stderr=stderr.decode(errors="replace")[-500:],
            )

        return read_result(raw)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """자식 프로세스를 정리한다 — 종료를 기다려 좀비를 남기지 않는다."""
        if process.returncode is not None:
            return
        process.kill()
        await process.wait()

    def _error(
        self,
        code: ErrorCode,
        category: ErrorCategory,
        message: str,
        task: TaskRequest,
        *,
        retryable: bool = False,
        **details: str,
    ) -> MalkuthError:
        """실패를 구조화 에러로."""
        return MalkuthError(
            category=category,
            code=code,
            message=message,
            agent=self.agent,
            task_id=task.task_id,
            retryable=retryable,
            details=details,
        )
