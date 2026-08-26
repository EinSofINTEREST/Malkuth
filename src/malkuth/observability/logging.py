"""Structured logging configuration.

structlog 기반 구조화 로깅. ``run_id`` 하나로 orchestrator → runtime → agentd →
protocol 로그를 관통해 추적할 수 있어야 하므로, 필드 이름을 상수로 고정한다.

로그 메시지는 영어로 쓰고 f-string 보간을 하지 않는다 — 값은 필드로 분리해야
기계 판독과 검색이 가능하다.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, MutableMapping

DEFAULT_LOG_LEVEL = "INFO"

REDACTED: Final = "***"


class LogField:
    """Standard structured log field names.

    표준 구조화 필드 이름. 05 의 필드 표와 1:1 대응하며, 의미가 중복되는 변형
    (``agent_name`` 등) 을 만들지 않기 위해 상수로 고정한다.
    """

    AGENT: Final = "agent"
    AGENT_VERSION: Final = "agent_version"
    GROUP: Final = "group"
    GRAPH: Final = "graph"
    RUN_ID: Final = "run_id"
    TASK_ID: Final = "task_id"
    NODE_ID: Final = "node_id"
    A2A_CALLER: Final = "a2a_caller"
    A2A_CALLEE: Final = "a2a_callee"
    A2A_TASK_ID: Final = "a2a_task_id"
    MCP_SERVER: Final = "mcp_server"
    TOOL: Final = "tool"
    SKILLSET: Final = "skillset"
    PROMPTSET: Final = "promptset"
    MODULE_REF: Final = "module_ref"
    MEMORY_SPACE: Final = "memory_space"
    MODEL: Final = "model"
    PROVIDER: Final = "provider"
    CONTAINER_ID: Final = "container_id"
    IMAGE: Final = "image"
    ERROR_CODE: Final = "error_code"
    STATUS: Final = "status"
    DURATION_MS: Final = "duration_ms"
    DELAY_MS: Final = "delay_ms"
    ATTEMPT: Final = "attempt"
    MAX_ATTEMPTS: Final = "max_attempts"
    INPUT_TOKENS: Final = "input_tokens"
    OUTPUT_TOKENS: Final = "output_tokens"
    TURN: Final = "turn"
    ITERATION: Final = "iteration"
    MODE: Final = "mode"
    PORT: Final = "port"


STANDARD_FIELDS: Final[frozenset[str]] = frozenset(
    value
    for name, value in vars(LogField).items()
    if not name.startswith("_") and isinstance(value, str)
)
"""표준 필드 이름 집합 — 컴포넌트가 임의 키를 만들지 않았는지 검증할 때 쓴다."""


# 값이 secret 인 것으로 간주할 키 패턴 — 이름으로 판정해 값 형태에 의존하지 않는다
_SECRET_KEY_PATTERN: Final = re.compile(
    r"(secret|token|password|passwd|api_?key|credential|authorization|private_?key)",
    re.IGNORECASE,
)

_MAX_REDACT_DEPTH: Final = 6

# 문자열 안에 섞여 들어온 secret — `token=sk-...`, `"api_key": "..."`, `Bearer ...`
# 형태를 값째로 가린다. 예외 메시지/트레이스백은 키 이름이 없는 자유 문자열이라
# 이름 기반 판정만으로는 잡히지 않는다.
_SECRET_VALUE_PATTERN: Final = re.compile(
    r"""
    (?P<prefix>
        (?:secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)
        ["']?            # JSON 형태의 닫는 따옴표: "password":
        \s* [=:] \s*
        ["']?            # 값을 여는 따옴표
    )
    (?P<value>[^\s"',;)}\]]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BEARER_PATTERN: Final = re.compile(
    r"\b(?P<scheme>bearer|basic)\s+(?P<value>[A-Za-z0-9._\-+/=]+)",
    re.IGNORECASE,
)


def _looks_secret(key: str) -> bool:
    """키 이름이 secret 을 담는 것으로 보이는지."""
    return bool(_SECRET_KEY_PATTERN.search(key))


def _redact_text(text: str) -> str:
    """자유 문자열 안에 섞인 secret 을 가린다.

    예외 메시지와 트레이스백은 키-값 구조가 아니므로 이름 기반 판정으로는
    잡히지 않는다 — 값 패턴으로 한 번 더 훑는다.
    """
    redacted = _SECRET_VALUE_PATTERN.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
    return _BEARER_PATTERN.sub(lambda m: f"{m.group('scheme')} {REDACTED}", redacted)


def _redact(value: Any, depth: int = 0) -> Any:
    """중첩 구조를 따라가며 secret 키의 값을 가린다."""
    if depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED if _looks_secret(str(key)) else _redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, depth + 1) for item in value)
    return value


def mask_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact secret-looking values from a log event.

    Secret 으로 보이는 키의 값을 가립니다 — 중첩 dict/list 까지 따라갑니다.

    두 층으로 판정합니다:

    1. **키 이름** — ``api_key`` 같은 키의 값을 통째로 가린다
    2. **문자열 값** — ``token=...`` / ``Bearer ...`` 처럼 자유 문자열 안에 섞인
       secret 을 가린다. 예외 메시지와 트레이스백은 키-값 구조가 아니므로
       이 층이 없으면 ``log.exception()`` 한 번으로 값이 그대로 새어나간다
    """
    for key in list(event_dict):
        if _looks_secret(str(key)):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def configure(
    *,
    level: str = DEFAULT_LOG_LEVEL,
    json_output: bool = True,
    extra_processors: Iterable[Any] | None = None,
) -> None:
    """Configure structlog for the process.

    프로세스 전역 로깅을 설정합니다.

    Args:
        level: Minimum level name (e.g. ``"INFO"``).
        json_output: JSON renderer for production; pretty console when False.
        extra_processors: Processors inserted before rendering.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # 마스킹은 렌더 직전에 — 그 사이에 어떤 processor 도 값을 되살리지 못하게
        *(extra_processors or []),
        mask_secrets,
        renderer,
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a bound logger.

    바인딩 가능한 로거를 반환합니다.
    """
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_agent(
    logger: Any, *, agent: str, version: str | None = None, group: str | None = None
) -> Any:
    """Bind the fields every ``runtime/`` and ``agentd/`` log must carry.

    runtime/agentd 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.AGENT: agent}
    if version is not None:
        bindings[LogField.AGENT_VERSION] = version
    if group is not None:
        bindings[LogField.GROUP] = group
    return logger.bind(**bindings)


def bind_run(logger: Any, *, graph: str, run_id: str, mode: str | None = None) -> Any:
    """Bind the fields every ``orchestrator/`` log must carry.

    orchestrator 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.GRAPH: graph, LogField.RUN_ID: run_id}
    if mode is not None:
        bindings[LogField.MODE] = mode
    return logger.bind(**bindings)


def bind_task(logger: Any, *, agent: str, task_id: str, node_id: str | None = None) -> Any:
    """Bind the fields every ``agentd/`` task log must carry.

    agentd 태스크 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.AGENT: agent, LogField.TASK_ID: task_id}
    if node_id is not None:
        bindings[LogField.NODE_ID] = node_id
    return logger.bind(**bindings)


def bind_a2a(logger: Any, *, caller: str, callee: str, task_id: str | None = None) -> Any:
    """Bind the fields every ``protocols/a2a/`` log must carry.

    A2A 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {
        LogField.A2A_CALLER: caller,
        LogField.A2A_CALLEE: callee,
    }
    if task_id is not None:
        bindings[LogField.A2A_TASK_ID] = task_id
    return logger.bind(**bindings)


def bind_mcp(logger: Any, *, agent: str, server: str, tool: str | None = None) -> Any:
    """Bind the fields every ``protocols/mcp/`` log must carry.

    MCP 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.AGENT: agent, LogField.MCP_SERVER: server}
    if tool is not None:
        bindings[LogField.TOOL] = tool
    return logger.bind(**bindings)


def bind_module(logger: Any, *, module_ref: str) -> Any:
    """Bind the field every ``modules/`` log must carry.

    모듈 레이어 로그의 필수 필드를 바인딩합니다.
    """
    return logger.bind(**{LogField.MODULE_REF: module_ref})
