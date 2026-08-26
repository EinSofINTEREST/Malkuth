"""Observability — structured logging and metrics.

관측성. 메트릭 이름은 대시보드·알림이 의존하는 계약이므로 고정된다.
"""

from malkuth.observability.metrics import (
    DEFAULT_METRICS_PORT,
    METRIC_SPECS,
    Metrics,
    MetricSpec,
    snapshot,
    start_metrics_server,
)

__all__ = [
    "DEFAULT_METRICS_PORT",
    "METRIC_SPECS",
    "MetricSpec",
    "Metrics",
    "snapshot",
    "start_metrics_server",
]
