"""Where identities and rules live so a restarted control plane still knows them.

신원과 부여·회수 기록을 저장한다. 에이전트 신원은 control plane 재시작을 넘어야 한다 — 떠 있는
컨테이너의 자격이 재시작마다 무효가 되면 모든 강제 지점이 그 에이전트를 거부한다 (09 Access
Enforcement 4).

자격 **값**은 저장하지 않는다. 해시만 둔다: 저장 파일이 새도 그것으로 에이전트를 사칭할 수 없다.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from malkuth.access.model import Effect, Mode, ResourceKind, Rule
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@dataclass(frozen=True)
class Identity:
    """An issued agent credential, by its hash."""

    credential_hash: str
    agent: str
    deployment_id: str
    issued_at: float
    revoked_at: float | None = None


@runtime_checkable
class AccessStore(Protocol):
    def put_identity(self, identity: Identity) -> None: ...
    def identity(self, credential_hash: str) -> Identity | None: ...
    def revoke_identities(self, deployment_id: str, at: float) -> int:
        """Revoke a deployment's identities; bumps the version **in the same write** if any."""
        ...

    def put_rule(self, rule: Rule) -> int:
        """Store a rule and bump the version **in the same write**; returns the new version.

        기록과 버전을 따로 쓰면 그 사이에 죽었을 때 기록은 바뀌고 버전은 그대로 남는다 — 재시작한
        레지스트리가 알림을 내지 않아 강제 지점이 옛 판정을 계속 쓴다.
        """
        ...

    def rule(self, rule_id: str) -> Rule | None: ...
    def rules(self, agent: str) -> Sequence[Rule]: ...
    def version(self) -> int: ...


@dataclass
class InMemoryAccessStore:
    """테스트/단일 프로세스용."""

    _identities: dict[str, Identity] = field(default_factory=dict)
    _rules: dict[str, Rule] = field(default_factory=dict)
    _version: int = 0

    def put_identity(self, identity: Identity) -> None:
        self._identities[identity.credential_hash] = identity

    def identity(self, credential_hash: str) -> Identity | None:
        return self._identities.get(credential_hash)

    def revoke_identities(self, deployment_id: str, at: float) -> int:
        revoked = 0
        for key, found in list(self._identities.items()):
            if found.deployment_id == deployment_id and found.revoked_at is None:
                self._identities[key] = replace(found, revoked_at=at)
                revoked += 1
        if revoked:
            self._version += 1
        return revoked

    def put_rule(self, rule: Rule) -> int:
        self._rules[rule.rule_id] = rule
        self._version += 1
        return self._version

    def rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def rules(self, agent: str) -> Sequence[Rule]:
        return sorted((r for r in self._rules.values() if r.agent == agent), key=_order)

    def version(self) -> int:
        return self._version


def _order(rule: Rule) -> tuple[float, str]:
    return (rule.created_at, rule.rule_id)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    credential_hash TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    deployment_id   TEXT NOT NULL,
    issued_at       REAL NOT NULL,
    revoked_at      REAL
);
CREATE INDEX IF NOT EXISTS identities_by_deployment ON identities (deployment_id);
CREATE TABLE IF NOT EXISTS rules (
    rule_id      TEXT PRIMARY KEY,
    agent        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    target       TEXT NOT NULL,
    mode         TEXT,
    effect       TEXT NOT NULL,
    decided_by   TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL,
    lifted_at    REAL
);
CREATE INDEX IF NOT EXISTS rules_by_agent ON rules (agent);
CREATE TABLE IF NOT EXISTS registry_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO registry_version (id, version) VALUES (1, 0);
"""


@dataclass
class SqliteAccessStore:
    """파일 하나 — 신원과 기록이 프로세스 재시작을 넘는다. 기록은 지우지 않는다."""

    path: str | Path
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(
                    str(self.path), isolation_level=None, check_same_thread=False
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.executescript(_SCHEMA)
            except sqlite3.Error as err:
                raise MalkuthError(
                    category=ErrorCategory.STORAGE,
                    code=ErrorCode.STOR_003,
                    message="access store could not be opened",
                    details={"path": str(self.path)},
                ) from err
        return self._conn

    def put_identity(self, identity: Identity) -> None:
        with self._lock:
            self._connect().execute(
                "INSERT OR REPLACE INTO identities VALUES (?,?,?,?,?)",
                (
                    identity.credential_hash,
                    identity.agent,
                    identity.deployment_id,
                    identity.issued_at,
                    identity.revoked_at,
                ),
            )

    def identity(self, credential_hash: str) -> Identity | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM identities WHERE credential_hash = ?", (credential_hash,))
                .fetchone()
            )
        return Identity(**dict(row)) if row else None

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """한 번에 커밋하거나 아무것도 남기지 않는다 — 연결은 autocommit 이라 직접 연다."""
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except sqlite3.Error as err:
                conn.execute("ROLLBACK")
                raise MalkuthError(
                    category=ErrorCategory.STORAGE,
                    code=ErrorCode.STOR_003,
                    message="access store write failed",
                    details={"path": str(self.path)},
                ) from err
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def revoke_identities(self, deployment_id: str, at: float) -> int:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE identities SET revoked_at = ? "
                "WHERE deployment_id = ? AND revoked_at IS NULL",
                (at, deployment_id),
            )
            if cursor.rowcount:
                _bump(conn)
        return cursor.rowcount

    def put_rule(self, rule: Rule) -> int:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rule.rule_id,
                    rule.agent,
                    rule.kind.value,
                    rule.target,
                    rule.mode.value if rule.mode else None,
                    rule.effect.value,
                    rule.decided_by,
                    rule.requested_by,
                    rule.reason,
                    rule.created_at,
                    rule.expires_at,
                    rule.lifted_at,
                ),
            )
            return _bump(conn)

    def rule(self, rule_id: str) -> Rule | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,))
                .fetchone()
            )
        return _rule(row) if row else None

    def rules(self, agent: str) -> Sequence[Rule]:
        with self._lock:
            rows = (
                self._connect()
                .execute(
                    "SELECT * FROM rules WHERE agent = ? ORDER BY created_at, rule_id", (agent,)
                )
                .fetchall()
            )
        return [_rule(row) for row in rows]

    def version(self) -> int:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT version FROM registry_version WHERE id = 1")
                .fetchone()
            )
        return int(row["version"])


def _bump(conn: sqlite3.Connection) -> int:
    updated = conn.execute("UPDATE registry_version SET version = version + 1 WHERE id = 1")
    if updated.rowcount != 1:
        raise sqlite3.DatabaseError("registry version row is missing")
    row = conn.execute("SELECT version FROM registry_version WHERE id = 1").fetchone()
    return int(row["version"])


def _rule(row: sqlite3.Row) -> Rule:
    return Rule(
        rule_id=row["rule_id"],
        agent=row["agent"],
        kind=ResourceKind(row["kind"]),
        target=row["target"],
        mode=Mode(row["mode"]) if row["mode"] else None,
        effect=Effect(row["effect"]),
        decided_by=row["decided_by"],
        requested_by=row["requested_by"],
        reason=row["reason"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        lifted_at=row["lifted_at"],
    )


__all__ = ["AccessStore", "Identity", "InMemoryAccessStore", "SqliteAccessStore"]
