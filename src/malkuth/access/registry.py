"""The access registry — identities, rules, ceilings, and decisions.

01 Access Control 의 판정 지점. 판정 순서:

1. 운영자의 **회수**가 해당하면 거부 — 선언도 부여도 이기지 못한다
2. **선언**(기본 권한)이 허용하면 허용
3. 권한 에이전트의 유효한 **부여**가 해당하면 허용
4. 그 밖에는 거부

넓히는 것은 권한 에이전트만, 그리고 **확장 상한** 안에서만 한다. 상한은 여기서 결정적으로
강제한다 — 확장 요청은 신뢰할 수 없는 입력이다 (03/09).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from malkuth.access.model import (
    DECLARATION,
    OPERATOR,
    Decision,
    Effect,
    Mode,
    Outcome,
    ResourceKind,
    Rule,
)
from malkuth.access.store import Identity
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import RESERVED_GLOBAL_GROUP, AccessCeiling

if TYPE_CHECKING:
    from malkuth.access.store import AccessStore
    from malkuth.catalog import Catalog
    from malkuth.observability.metrics import Metrics

log = structlog.get_logger(__name__)

ACCESS_CREDENTIAL_ENV = "MALKUTH_ACCESS_CREDENTIAL"  # noqa: S105 — 키 이름이지 값이 아니다
"""에이전트 컨테이너에 주입되는 신원 자격의 env 이름."""

DEFAULT_OUTCOME_SOURCE = "default"
"""어느 규칙에도 해당하지 않아 거부된 판정의 출처."""


@runtime_checkable
class Baseline(Protocol):
    """What declarations allow for one resource kind.

    선언된 기본 권한. 종류마다 선언이 사는 곳이 다르다 — 메모리는 매니페스트·그룹, A2A 는 그래프의
    ``connections`` — 그래서 종류별로 꽂는다.
    """

    def allows(self, agent: str, target: str, mode: Mode | None) -> bool: ...


def credential_hash(credential: str) -> str:
    """저장하고 대조하는 것은 해시다 — 저장 파일이 새도 사칭할 수 없다."""
    return hashlib.sha256(credential.encode()).hexdigest()


def _refused(message: str, **details: Any) -> MalkuthError:
    return MalkuthError(
        category=ErrorCategory.FORBIDDEN,
        code=ErrorCode.ACC_003,
        message=message,
        details=details,
    )


def _unknown_identity() -> MalkuthError:
    return MalkuthError(
        category=ErrorCategory.FORBIDDEN,
        code=ErrorCode.ACC_001,
        message="not a recognised agent credential",
    )


@dataclass
class AccessRegistry:
    """Decides who may do what, and records every change.

    Attributes:
        store: 신원과 기록 — 재시작을 넘는다.
        catalog: 확장 상한(그룹·global 선언)과 에이전트 소속의 출처.
        stewards: 권한 에이전트로 지정된 에이전트 이름 — **운영자 설정**이다. 에이전트가 스스로
            자신을 지정할 방법은 없다.
        baselines: 종류별 선언 기본 권한. 없는 종류는 선언이 아무것도 허용하지 않는다.
        clock: epoch 초 — 테스트는 주입한다 (06).
    """

    store: AccessStore
    catalog: Catalog
    stewards: frozenset[str] = frozenset()
    baselines: Mapping[ResourceKind, Baseline] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    metrics: Metrics | None = None
    _changed: asyncio.Event | None = field(default=None, init=False, repr=False)

    # --- 신원 ---------------------------------------------------------------

    def issue_identity(self, agent: str, deployment_id: str) -> str:
        """Mint a credential for one agent in one deployment and return it once.

        값은 여기서 한 번만 돌려준다 — 레지스트리는 해시만 저장한다.
        """
        credential = secrets.token_urlsafe(32)
        self.store.put_identity(
            Identity(
                credential_hash=credential_hash(credential),
                agent=agent,
                deployment_id=deployment_id,
                issued_at=self.clock(),
            )
        )
        log.info("agent identity issued", agent=agent, deployment_id=deployment_id)
        return credential

    def revoke_deployment(self, deployment_id: str) -> int:
        """배포를 해체하면 그 신원은 더 이상 아무 강제 지점도 통과하지 못한다."""
        revoked = self.store.revoke_identities(deployment_id, self.clock())
        if revoked:
            self._wake_waiters()
            log.info("agent identities revoked", deployment_id=deployment_id, count=revoked)
        return revoked

    def identify(self, credential: str) -> str:
        """The agent behind a credential.

        Raises:
            MalkuthError: FORBIDDEN/``ACC_001`` if the credential is unknown or revoked.
        """
        found = self.store.identity(credential_hash(credential)) if credential else None
        if found is None or found.revoked_at is not None:
            raise _unknown_identity()
        return found.agent

    # --- 판정 ---------------------------------------------------------------

    def decide(
        self, agent: str, kind: ResourceKind, target: str, mode: Mode | None = None
    ) -> Decision:
        """Decide one request — revocation, then declaration, then grant."""
        now = self.clock()
        active = [
            r for r in self.store.rules(agent) if r.active(now) and r.covers(kind, target, mode)
        ]
        version = self.store.version()

        def answer(outcome: Outcome, decided_by: str) -> Decision:
            self._count_decision(kind, outcome)
            return Decision(agent, kind, target, mode, outcome, decided_by, version)

        denial = next((r for r in active if r.effect is Effect.DENY), None)
        if denial is not None:
            return answer(Outcome.DENY, denial.decided_by)
        baseline = self.baselines.get(kind)
        if baseline is not None and baseline.allows(agent, target, mode):
            return answer(Outcome.ALLOW, DECLARATION)
        grant = next((r for r in active if r.effect is Effect.ALLOW), None)
        if grant is not None:
            return answer(Outcome.ALLOW, grant.decided_by)
        return answer(Outcome.DENY, DEFAULT_OUTCOME_SOURCE)

    # --- 운영자: 좁히기 --------------------------------------------------------

    def revoke(
        self,
        agent: str,
        kind: ResourceKind,
        target: str,
        *,
        reason: str,
        mode: Mode | None = None,
        expires_in_s: float | None = None,
    ) -> Rule:
        """Narrow an agent's permission — declared ones included (operator only).

        ``mode=rw`` 로 memory 를 회수하면 쓰기만 막고 읽기는 남긴다 (rw → ro 강등).
        """
        self.catalog.agent(agent)  # 없는 에이전트에 대한 기록은 만들지 않는다
        now = self.clock()
        rule = Rule(
            rule_id=f"rule-{uuid.uuid4().hex[:16]}",
            agent=agent,
            kind=kind,
            target=target,
            mode=mode,
            effect=Effect.DENY,
            decided_by=OPERATOR,
            reason=reason,
            created_at=now,
            expires_at=None if expires_in_s is None else now + expires_in_s,
        )
        return self._record(rule, op="revoke")

    def lift(self, rule_id: str) -> Rule:
        """End a rule early — restores a revocation, or cuts a grant short (operator only).

        Raises:
            MalkuthError: NOT_FOUND/``NF_001`` if no such rule.
        """
        found = self.store.rule(rule_id)
        if found is None:
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown access rule",
                details={"rule_id": rule_id},
            )
        if found.lifted_at is not None:
            return found
        return self._record(replace(found, lifted_at=self.clock()), op="lift")

    # --- 권한 에이전트: 넓히기 ---------------------------------------------------

    def grant(
        self,
        steward_credential: str,
        agent: str,
        kind: ResourceKind,
        target: str,
        *,
        ttl_s: float,
        reason: str,
        requested_by: str,
        mode: Mode | None = None,
    ) -> Rule:
        """Widen an agent's permission within its expansion ceiling.

        Raises:
            MalkuthError: FORBIDDEN/``ACC_001`` unknown credential,
                FORBIDDEN/``ACC_003`` the caller is not a permission agent, grants to itself,
                exceeds the ceiling or its TTL, or the permission is revoked by an operator,
                NOT_FOUND/``NF_001`` unknown agent.
        """
        steward = self.identify(steward_credential)
        try:
            self._check_grant(steward, agent, kind, target, mode, ttl_s)
        except MalkuthError as err:
            if err.code == ErrorCode.ACC_003:
                self._count_grant(kind, "refuse", steward)
                log.warning(
                    "access grant refused",
                    agent=agent,
                    resource=kind.value,
                    target=target,
                    decided_by=steward,
                    error_code=str(err.code),
                    reason=err.message,
                )
            raise
        now = self.clock()
        rule = Rule(
            rule_id=f"rule-{uuid.uuid4().hex[:16]}",
            agent=agent,
            kind=kind,
            target=target,
            mode=mode,
            effect=Effect.ALLOW,
            decided_by=steward,
            requested_by=requested_by,
            reason=reason,
            created_at=now,
            expires_at=now + ttl_s,
        )
        return self._record(rule, op="grant")

    def _check_grant(
        self,
        steward: str,
        agent: str,
        kind: ResourceKind,
        target: str,
        mode: Mode | None,
        ttl_s: float,
    ) -> None:
        if steward not in self.stewards:
            raise _refused("only a permission agent may grant", caller=steward)
        if steward == agent:
            raise _refused("a permission agent may not grant to itself", agent=agent)
        self.catalog.agent(agent)
        if kind is ResourceKind.MEMORY and mode is None:
            raise _refused("a memory grant must name its mode", agent=agent)
        ceiling = self._ceiling_for(agent, kind, target, mode)
        if ceiling is None:
            raise _refused(
                "grant exceeds the expansion ceiling",
                agent=agent,
                resource=kind.value,
                target=target,
            )
        if not 0 < ttl_s <= ceiling.max_ttl_s:
            raise _refused(
                "grant lifetime exceeds the ceiling",
                agent=agent,
                ttl_s=ttl_s,
                max_ttl_s=ceiling.max_ttl_s,
            )
        now = self.clock()
        if any(
            r.effect is Effect.DENY and r.active(now) and r.covers(kind, target, mode)
            for r in self.store.rules(agent)
        ):
            # 운영자가 회수한 것을 권한 에이전트가 되살리지 못한다 — 되돌리는 것은 운영자다
            raise _refused("permission is revoked by an operator", agent=agent, target=target)

    def _ceiling_for(
        self, agent: str, kind: ResourceKind, target: str, mode: Mode | None
    ) -> AccessCeiling | None:
        """요청을 덮는 상한 — 소속 그룹과 global 의 선언에서 찾는다. 없으면 None."""
        group = self.catalog.agent(agent).metadata.group
        names = [RESERVED_GLOBAL_GROUP] + ([group] if group else [])
        for name in names:
            try:
                ceiling = self.catalog.group(name).spec.access.ceiling
            except MalkuthError as err:
                if err.code == ErrorCode.NF_001:
                    continue
                raise
            if ceiling is not None and _ceiling_covers(ceiling, kind, target, mode):
                return ceiling
        return None

    # --- 조회와 변경 알림 --------------------------------------------------------

    def rules(self, agent: str) -> list[Rule]:
        return list(self.store.rules(agent))

    def version(self) -> int:
        return self.store.version()

    async def wait_for_change(self, after: int, timeout_s: float) -> int:
        """Return the registry version once it moves past ``after``, or at the timeout.

        강제 지점의 변경 알림 (01 Access Control — 캐시 + 변경 알림). 긴 폴링이라 알림을 받는 쪽이
        연결을 끊어도 레지스트리에 남는 상태가 없다.
        """
        current = self.store.version()
        if current > after:
            return current
        if self._changed is None:
            self._changed = asyncio.Event()
        event = self._changed
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        return self.store.version()

    # --- 내부 ---------------------------------------------------------------

    def _record(self, rule: Rule, *, op: str) -> Rule:
        self.store.put_rule(rule)
        self._wake_waiters()
        self._count_grant(rule.kind, op, rule.decided_by)
        log.info(
            "access rule recorded",
            agent=rule.agent,
            resource=rule.kind.value,
            target=rule.target,
            grant_id=rule.rule_id,
            decided_by=rule.decided_by,
            op=op,
            effect=rule.effect.value,
        )
        return rule

    def _wake_waiters(self) -> None:
        """버전은 저장소가 기록과 함께 올렸다 — 여기서는 기다리는 쪽만 깨운다."""
        if self._changed is not None:
            # 기다리던 쪽을 모두 깨우고, 다음 대기는 새 이벤트로 — set 된 채 두면 계속 깨어난다
            self._changed.set()
            self._changed = None

    def _count_decision(self, kind: ResourceKind, outcome: Outcome) -> None:
        if self.metrics is not None:
            self.metrics.counter("malkuth_access_decisions_total").labels(
                resource=kind.value, decision=outcome.value, source="fresh"
            ).inc()

    def _count_grant(self, kind: ResourceKind, op: str, decided_by: str) -> None:
        if self.metrics is not None:
            self.metrics.counter("malkuth_access_grants_total").labels(
                resource=kind.value, op=op, decided_by=decided_by
            ).inc()


def _ceiling_covers(
    ceiling: AccessCeiling, kind: ResourceKind, target: str, mode: Mode | None
) -> bool:
    if kind is ResourceKind.MEMORY:
        return any(
            entry.space == target and (entry.mode.value == "rw" or mode is not Mode.RW)
            for entry in ceiling.memory
        )
    listed = {
        ResourceKind.EGRESS: ceiling.egress,
        ResourceKind.MCP_TOOL: ceiling.mcp_tool,
        ResourceKind.A2A: ceiling.a2a,
    }[kind]
    return target in listed


__all__ = [
    "ACCESS_CREDENTIAL_ENV",
    "AccessRegistry",
    "Baseline",
    "credential_hash",
]
