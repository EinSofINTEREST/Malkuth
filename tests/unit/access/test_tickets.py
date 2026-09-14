"""A2A call tickets and the declared-connection check (#281).

저장소의 `research-pipeline` 은 researcher → planner 만 선언한다. 역방향은 선언이 없다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from malkuth.access.baselines import A2ABaseline
from malkuth.access.model import ResourceKind
from malkuth.access.registry import INVALID_TICKET, TICKET_TTL_S, AccessRegistry
from malkuth.access.store import InMemoryAccessStore, SqliteAccessStore
from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode, MalkuthError

REPO_ROOT = Path(__file__).resolve().parents[3]
GRAPH = "research-pipeline"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(clock) -> AccessRegistry:
    catalog = Catalog.under(REPO_ROOT)
    store = InMemoryAccessStore()
    return AccessRegistry(
        store=store,
        catalog=catalog,
        baselines={ResourceKind.A2A: A2ABaseline(catalog, store)},
        clock=clock,
    )


@pytest.fixture
def agents(registry) -> dict[str, str]:
    return {
        name: registry.issue_identity(name, "dep-1", graph=GRAPH)
        for name in ("planner", "researcher", "writer")
    }


def verify(registry, agents, *, callee: str, caller: str = "researcher", ticket: str | None = None):
    if ticket is None:
        ticket, _ = registry.issue_ticket(agents[caller], callee)
    return registry.verify_ticket(agents[callee], ticket)


# --- 선언된 연결 ----------------------------------------------------------------


def test_a_declared_connection_is_allowed_by_the_callee(registry, agents):
    decision = verify(registry, agents, callee="planner")

    assert decision.allowed
    assert (decision.agent, decision.decided_by) == ("researcher", "declaration")


def test_the_reverse_direction_is_not_declared(registry, agents):
    """방향은 선언의 문제다 — planner → researcher 는 선언이 없다."""
    assert not verify(registry, agents, caller="planner", callee="researcher").allowed


def test_a_connection_in_a_graph_that_is_not_deployed_does_not_count(registry):
    """저장소에 그래프가 있어도 호출자가 그 그래프로 배포되지 않았으면 연결이 아니다."""
    caller = registry.issue_identity("researcher", "dep-9", graph="")
    callee = registry.issue_identity("planner", "dep-9", graph="")
    ticket, _ = registry.issue_ticket(caller, "planner")

    assert not registry.verify_ticket(callee, ticket).allowed


def test_revoking_the_connection_denies_the_next_call_and_lifting_restores_it(registry, agents):
    ticket, _ = registry.issue_ticket(agents["researcher"], "planner")
    rule = registry.revoke("researcher", ResourceKind.A2A, "planner", reason="incident")

    assert not registry.verify_ticket(agents["planner"], ticket).allowed

    registry.lift(rule.rule_id)
    assert registry.verify_ticket(agents["planner"], ticket).allowed


# --- 표가 증명하는 것 -------------------------------------------------------------


def test_a_ticket_only_works_for_the_callee_it_was_issued_for(registry, agents):
    """표를 받은 피호출자가 그것을 들고 호출자 행세를 하지 못한다."""
    ticket, _ = registry.issue_ticket(agents["researcher"], "planner")

    replayed = registry.verify_ticket(agents["writer"], ticket)

    assert not replayed.allowed and replayed.decided_by == INVALID_TICKET


def test_a_forged_or_expired_ticket_is_denied(registry, agents, clock):
    assert verify(registry, agents, callee="planner", ticket="forged").decided_by == INVALID_TICKET

    ticket, expires_at = registry.issue_ticket(agents["researcher"], "planner")
    assert expires_at == clock.now + TICKET_TTL_S
    clock.now = expires_at

    assert registry.verify_ticket(agents["planner"], ticket).decided_by == INVALID_TICKET


def test_a_ticket_dies_with_the_callers_identity(registry, agents):
    ticket, _ = registry.issue_ticket(agents["researcher"], "planner")

    registry.revoke_deployment("dep-1")
    live_planner = registry.issue_identity("planner", "dep-2", graph=GRAPH)

    assert registry.verify_ticket(live_planner, ticket).decided_by == INVALID_TICKET


def test_a_decision_is_cached_no_longer_than_the_ticket_lives(registry, agents):
    ticket, expires_at = registry.issue_ticket(agents["researcher"], "planner")

    assert registry.verify_ticket(agents["planner"], ticket).valid_until == expires_at


def test_a_ticket_is_the_callers_own_identity_and_not_a_credential(registry, agents):
    """표로는 신원이 되지 않는다 — Memory Service 같은 다른 강제 지점에서 쓸 수 없다."""
    ticket, _ = registry.issue_ticket(agents["researcher"], "planner")

    with pytest.raises(MalkuthError) as exc_info:
        registry.identify(ticket)

    assert exc_info.value.code == ErrorCode.ACC_001


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda r, a: r.issue_ticket("made-up", "planner"), ErrorCode.ACC_001),
        (lambda r, a: r.issue_ticket(a["researcher"], "ghost"), ErrorCode.NF_001),
        (lambda r, a: r.verify_ticket("made-up", "any"), ErrorCode.ACC_001),
    ],
)
def test_tickets_need_a_real_caller_callee_and_verifier(registry, agents, call, code):
    with pytest.raises(MalkuthError) as exc_info:
        call(registry, agents)

    assert exc_info.value.code == code


# --- 저장소 ----------------------------------------------------------------------


def test_tickets_and_deployed_graphs_survive_a_restart(tmp_path):
    path = tmp_path / "access.db"
    catalog = Catalog.under(REPO_ROOT)
    first = AccessRegistry(store=SqliteAccessStore(path=path), catalog=catalog)
    caller = first.issue_identity("researcher", "dep-1", graph=GRAPH)
    callee = first.issue_identity("planner", "dep-1", graph=GRAPH)
    ticket, _ = first.issue_ticket(caller, "planner")

    store = SqliteAccessStore(path=path)
    second = AccessRegistry(
        store=store, catalog=catalog, baselines={ResourceKind.A2A: A2ABaseline(catalog, store)}
    )

    assert second.verify_ticket(callee, ticket).allowed
    assert ticket not in "\n".join(sqlite3.connect(path).iterdump()), "표 값을 저장했다"


def test_a_store_from_before_graphs_opens_and_keeps_its_identities(tmp_path):
    """#277 에 만든 파일에는 identities.graph 가 없다."""
    path = tmp_path / "access.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE identities (credential_hash TEXT PRIMARY KEY, agent TEXT NOT NULL, "
            "deployment_id TEXT NOT NULL, issued_at REAL NOT NULL, revoked_at REAL)"
        )
        conn.execute("INSERT INTO identities VALUES ('h', 'planner', 'dep-0', 1.0, NULL)")

    store = SqliteAccessStore(path=path)

    found = store.identity("h")
    assert found is not None and (found.agent, found.graph) == ("planner", "")
    assert store.live_graphs("planner") == frozenset()
