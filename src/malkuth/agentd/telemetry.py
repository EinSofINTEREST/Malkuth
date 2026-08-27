"""Task-loop metric collection.

에이전트 실행 루프의 메트릭 집계. Executor 본문이 계측 코드로 덮이지 않도록
별도 콜라보레이터로 분리한다 — 주입하지 않으면 아무 것도 기록하지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from malkuth.core.tools import is_mcp_tool

if TYPE_CHECKING:
    from malkuth.core.agent import ModelUsage
    from malkuth.observability.metrics import Metrics

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"

SOURCE_MCP: Final = "mcp"
SOURCE_SKILLSET: Final = "skillset"

DIRECTION_INPUT: Final = "input"
DIRECTION_OUTPUT: Final = "output"


def tool_source(name: str) -> str:
    """tool 의 출처 — 네임스페이스 접두사로 판별한다 (05 표준 라벨)."""
    return SOURCE_MCP if is_mcp_tool(name) else SOURCE_SKILLSET


class ExecutorTelemetry:
    """Records the agent-execution metric contract.

    태스크·모델·tool 경로의 표준 메트릭을 기록합니다. ``agent`` 이외의 라벨은
    태스크가 아니라 배치 시점에 정해지므로 (에이전트가 어느 그룹·그래프에
    속하는지) 생성 시 받습니다.
    """

    def __init__(
        self,
        metrics: Metrics,
        *,
        agent: str,
        group: str = "",
        graph: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self._metrics = metrics
        self._agent = agent
        self._group = group
        self._graph = graph
        self._provider = provider
        self._model = model

    def task_finished(self, *, status: str, duration_s: float) -> None:
        """태스크 종료 — 상태별 카운터와 latency 히스토그램."""
        self._metrics.counter("malkuth_agent_tasks_total").labels(
            agent=self._agent, group=self._group, graph=self._graph, status=status
        ).inc()
        self._metrics.histogram("malkuth_agent_task_duration_seconds").labels(
            agent=self._agent, group=self._group, graph=self._graph
        ).observe(duration_s)

    def model_called(self, *, status: str, usage: ModelUsage | None = None) -> None:
        """모델 호출 한 턴 — 요청 카운터와 방향별 토큰."""
        self._metrics.counter("malkuth_model_requests_total").labels(
            agent=self._agent, provider=self._provider, model=self._model, status=status
        ).inc()
        if usage is None:
            return
        tokens = self._metrics.counter("malkuth_model_tokens_total")
        for direction, count in (
            (DIRECTION_INPUT, usage.input_tokens),
            (DIRECTION_OUTPUT, usage.output_tokens),
        ):
            if count:
                tokens.labels(agent=self._agent, model=self._model, direction=direction).inc(count)

    def tool_called(self, *, tool: str, status: str) -> None:
        """tool 호출 — skillset/mcp 를 source 라벨로 구분한다."""
        self._metrics.counter("malkuth_tool_calls_total").labels(
            agent=self._agent, source=tool_source(tool), tool=tool, status=status
        ).inc()
