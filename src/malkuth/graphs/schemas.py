"""Graph state schemas.

그래프 state pydantic 모델. 토폴로지의 ``state.schema`` ref 대상이다.
노드 산출물은 ``output_map`` 으로만 병합되며, 선언되지 않은 키는 반영되지 않는다.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ResearchState(BaseModel):
    """State for the reference research pipeline.

    레퍼런스 리서치 파이프라인의 state.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    plan: str | None = None
    needs_research: bool = True
    findings: list[str] = Field(default_factory=list)
    report: str | None = None


class FeedMonitorState(BaseModel):
    """State for the reference service graph.

    레퍼런스 상주 그래프의 state — iteration 간 연속성이 필요한 데이터를 담는다.
    """

    model_config = ConfigDict(frozen=True)

    feeds: list[str] = Field(default_factory=list)
    new_items: list[str] = Field(default_factory=list)
    seen_ids: list[str] = Field(default_factory=list)
    notified: int = 0
