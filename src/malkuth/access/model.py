"""Permission records and decisions.

01 Access Control 의 권한 모델. 선언이 기본 권한이고, 레지스트리가 저장하는 것은 그 위에 얹는
두 가지뿐이다:

- **회수** (``deny``) — 운영자가 선언된 권한까지 좁힌다. 권한 에이전트의 부여보다 강하다
- **부여** (``allow``) — 권한 에이전트가 확장 상한 안에서, 만료와 함께 넓힌다
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResourceKind(StrEnum):
    """What a permission is about."""

    MEMORY = "memory"
    EGRESS = "egress"
    MCP_TOOL = "mcp_tool"
    A2A = "a2a"


class Mode(StrEnum):
    """Memory access mode — other kinds have none."""

    RO = "ro"
    RW = "rw"


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


OPERATOR = "operator"
"""운영자가 결정한 기록의 ``decided_by``."""

DECLARATION = "declaration"
"""선언에서 온 판정의 출처."""


@dataclass(frozen=True)
class Rule:
    """One stored grant or revocation.

    Attributes:
        rule_id: 기록 id.
        agent: 대상 에이전트.
        kind / target / mode: 무엇에 대한 것인가. ``mode`` 는 memory 만 쓴다.
            회수에서 ``mode=rw`` 는 "쓰기만 회수"(ro 로 강등), ``None``/``ro`` 는 전부 회수다.
        effect: 부여(allow) 또는 회수(deny).
        reason / requested_by / decided_by: 출처 — 모든 기록은 누가 왜 결정했는지 남긴다.
        created_at / expires_at / lifted_at: epoch 초. 부여는 만료가 필수다.
    """

    rule_id: str
    agent: str
    kind: ResourceKind
    target: str
    effect: Effect
    decided_by: str
    reason: str
    created_at: float
    mode: Mode | None = None
    requested_by: str = ""
    expires_at: float | None = None
    lifted_at: float | None = None

    def active(self, now: float) -> bool:
        if self.lifted_at is not None:
            return False
        return self.expires_at is None or now < self.expires_at

    def covers(self, kind: ResourceKind, target: str, mode: Mode | None) -> bool:
        """이 기록이 요청된 (종류, 대상, 모드) 에 해당하는가."""
        if self.kind is not kind or self.target != target:
            return False
        if kind is not ResourceKind.MEMORY:
            return True
        if self.effect is Effect.DENY:
            # 쓰기만 회수한 기록은 쓰기 요청에만 해당한다
            return self.mode is not Mode.RW or mode is Mode.RW
        # 부여는 요청 모드 이상이어야 한다 — ro 부여로 쓰기를 열지 않는다
        return self.mode is Mode.RW or mode is not Mode.RW


@dataclass(frozen=True)
class Decision:
    """The answer an enforcement point acts on."""

    agent: str
    kind: ResourceKind
    target: str
    mode: Mode | None
    outcome: Outcome
    decided_by: str
    """판정을 만든 것 — ``declaration`` / ``operator`` / 권한 에이전트 이름 / ``default``."""
    version: int
    """판정 시점의 레지스트리 버전 — 강제 지점이 캐시를 이 번호로 무효화한다."""

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW


__all__ = [
    "DECLARATION",
    "OPERATOR",
    "Decision",
    "Effect",
    "Mode",
    "Outcome",
    "ResourceKind",
    "Rule",
]
