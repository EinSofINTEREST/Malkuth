"""Observability — structured logging and metrics.

관측성. 로그는 run_id 로 전 레이어를 관통해 추적되고, 메트릭 이름은
대시보드·알림이 의존하는 계약이므로 고정된다.
"""

from malkuth.observability.logging import (
    REDACTED,
    STANDARD_FIELDS,
    LogField,
    bind_a2a,
    bind_agent,
    bind_mcp,
    bind_module,
    bind_run,
    bind_task,
    configure,
    get_logger,
    mask_secrets,
)

__all__ = [
    "REDACTED",
    "STANDARD_FIELDS",
    "LogField",
    "bind_a2a",
    "bind_agent",
    "bind_mcp",
    "bind_module",
    "bind_run",
    "bind_task",
    "configure",
    "get_logger",
    "mask_secrets",
]
