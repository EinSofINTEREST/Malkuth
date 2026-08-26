"""Unit tests for the memory store.

append-only 와 provenance 가 이 계층의 계약이다 — 과거 기록이 사후에 바뀌면
provenance 가 거짓말이 된다.
"""

from __future__ import annotations

import sqlite3

import pytest

from malkuth.core.errors import ErrorCategory, MalkuthError
from malkuth.memory.entry import MAX_CONTENT_BYTES, MemoryEntry, MemorySource
from malkuth.memory.store import SqliteMemoryStore
from malkuth.modules.memoryset import MemoryKind


@pytest.fixture
def store():
    """메모리 내 저장소 — finalizer 가 연결을 닫는다."""
    store = SqliteMemoryStore()
    try:
        yield store
    finally:
        store.close()


def make_entry(**overrides) -> MemoryEntry:
    base = {
        "space": "local:researcher:longterm",
        "kind": MemoryKind.FACT,
        "content": "mcp sidecar 는 이미지 태그 고정이 필요하다",
        "source": MemorySource(agent="researcher", run_id="run-1", task_id="task-1"),
    }
    base.update(overrides)
    return MemoryEntry(**base)


# --- 저장 --------------------------------------------------------------------


def test_append_stores_and_returns_the_entry(store):
    entry = make_entry()

    stored = store.append(entry)

    assert store.get(stored.entry_id) == entry


def test_entry_round_trips_through_the_row_form(store):
    """행 표현으로 갔다 와도 tags/source 가 보존되어야 한다."""
    entry = make_entry(tags=("mcp", "sidecar"), importance=0.9)

    store.append(entry)
    loaded = store.get(entry.entry_id)

    assert loaded is not None
    assert loaded.tags == ("mcp", "sidecar")
    assert loaded.source.agent == "researcher"
    assert loaded.importance == 0.9


def test_missing_entry_returns_none(store):
    assert store.get("absent") is None


# --- append-only --------------------------------------------------------------


def test_update_is_rejected_by_the_store(store):
    """계약을 코드 규율로만 지키면 결국 누군가 UPDATE 를 쓴다."""
    entry = make_entry()
    store.append(entry)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE memory_entries SET content = 'tampered' WHERE entry_id = ?",
            (entry.entry_id,),
        )


def test_correction_is_a_new_entry(store):
    """정정은 수정이 아니라 supersedes 를 단 새 항목이다."""
    original = make_entry(content="포트는 9000 이다")
    store.append(original)

    corrected = make_entry(content="포트는 9100 이다", supersedes=original.entry_id)
    store.append(corrected)

    assert store.get(original.entry_id).content == "포트는 9000 이다"
    assert store.latest_of_chain(original.entry_id).entry_id == corrected.entry_id


def test_correction_chain_follows_to_the_newest(store):
    first = make_entry(content="v1")
    store.append(first)
    second = make_entry(content="v2", supersedes=first.entry_id)
    store.append(second)
    third = make_entry(content="v3", supersedes=second.entry_id)
    store.append(third)

    assert store.latest_of_chain(first.entry_id).content == "v3"


def test_chain_of_an_unsuperseded_entry_is_itself(store):
    entry = make_entry()
    store.append(entry)

    assert store.latest_of_chain(entry.entry_id) == entry


def test_chain_of_unknown_entry_is_none(store):
    assert store.latest_of_chain("absent") is None


def test_cyclic_correction_does_not_hang(store):
    """순환 정정이 들어와도 무한 루프에 빠지지 않는다."""
    first = make_entry(content="a")
    store.append(first)
    second = make_entry(content="b", supersedes=first.entry_id)
    store.append(second)
    # first 가 second 를 대체한다고 주장하는 순환 구조를 직접 만든다
    cyclic = make_entry(entry_id=first.entry_id + "-cycle", supersedes=second.entry_id)
    store.append(cyclic)
    store._conn.execute(
        "INSERT INTO memory_entries "
        "(entry_id, space, kind, content, tags, agent, created_at, importance, supersedes) "
        "VALUES ('loop', ?, 'fact', 'loop', '', 'researcher', '2026-01-01T00:00:00+00:00', "
        " 0.5, ?)",
        (first.space, cyclic.entry_id),
    )
    store._conn.execute(
        "INSERT INTO memory_entries "
        "(entry_id, space, kind, content, tags, agent, created_at, importance, supersedes) "
        "VALUES ('loop2', ?, 'fact', 'loop2', '', 'researcher', '2026-01-01T00:00:00+00:00', "
        " 0.5, 'loop')",
        (first.space,),
    )
    store._conn.commit()

    result = store.latest_of_chain(first.entry_id)

    assert result is not None


# --- 불변식 ------------------------------------------------------------------


def test_provenance_is_required(store):
    """출처 없는 기억은 나중에 믿어도 되는지 판단할 근거가 없다."""
    entry = make_entry(source=MemorySource(agent=""))

    with pytest.raises(MalkuthError) as exc_info:
        store.append(entry)

    assert exc_info.value.code == "MEM_002"
    assert exc_info.value.category is ErrorCategory.MEMORY


def test_oversized_content_is_rejected(store):
    """대용량 원문은 artifact 로 — 검색 품질과 주입 비용이 함께 망가진다."""
    entry = make_entry(content="x" * (MAX_CONTENT_BYTES + 1))

    with pytest.raises(MalkuthError) as exc_info:
        store.append(entry)

    assert exc_info.value.code == "MEM_002"
    assert exc_info.value.details["limit_bytes"] == MAX_CONTENT_BYTES


def test_content_at_the_cap_is_accepted(store):
    entry = make_entry(content="x" * MAX_CONTENT_BYTES)

    assert store.append(entry).entry_id == entry.entry_id


def test_multibyte_content_is_measured_in_bytes(store):
    """한글은 문자 수와 바이트 수가 다르다 — 상한은 바이트 기준이다."""
    entry = make_entry(content="가" * (MAX_CONTENT_BYTES // 3 + 1))

    with pytest.raises(MalkuthError):
        store.append(entry)


# --- space 격리 ---------------------------------------------------------------


def test_listing_does_not_cross_space_boundaries(store):
    """인덱스/조회가 space 경계를 넘으면 격리가 무너진다."""
    store.append(make_entry(space="local:researcher:longterm", content="mine"))
    store.append(make_entry(space="local:writer:longterm", content="theirs"))

    entries = store.list_space("local:researcher:longterm")

    assert [e.content for e in entries] == ["mine"]


def test_listing_filters_by_kind(store):
    store.append(make_entry(kind=MemoryKind.FACT, content="f"))
    store.append(make_entry(kind=MemoryKind.OBSERVATION, content="o"))

    entries = store.list_space("local:researcher:longterm", kinds=[MemoryKind.OBSERVATION])

    assert [e.content for e in entries] == ["o"]


def test_listing_respects_the_limit(store):
    for i in range(5):
        store.append(make_entry(content=f"e{i}"))

    assert len(store.list_space("local:researcher:longterm", limit=2)) == 2


# --- retention 전용 삭제 -------------------------------------------------------


def test_purge_removes_entries(store):
    entry = make_entry()
    store.append(entry)

    removed = store.purge([entry.entry_id])

    assert removed == 1
    assert store.get(entry.entry_id) is None


def test_purge_of_nothing_is_a_noop(store):
    assert store.purge([]) == 0
