"""Unit tests for the hybrid memory index.

하이브리드가 기본인 이유는 한쪽만으로는 놓치기 때문이다 — 식별자는 lexical 이,
표현이 다른 같은 뜻은 vector 가 잡는다. 임베딩은 결정적 대역을 쓴다
(실 embedding API 호출 금지).
"""

from __future__ import annotations

import pytest

from malkuth.core.errors import MalkuthError
from malkuth.memory.embedding import HashEmbedder, cosine, normalize, tokenize
from malkuth.memory.entry import MemoryEntry, MemorySource
from malkuth.memory.index import (
    IndexQueue,
    IndexRegistry,
    SpaceIndex,
    split_chunks,
)
from malkuth.modules.memoryset import ChunkSpec, MemoryKind

SPACE = "local:researcher:longterm"


def entry(content: str, **overrides) -> MemoryEntry:
    base = {
        "space": SPACE,
        "kind": MemoryKind.FACT,
        "content": content,
        "source": MemorySource(agent="researcher"),
    }
    base.update(overrides)
    return MemoryEntry(**base)


def spec(**overrides) -> ChunkSpec:
    return ChunkSpec(**{"max_tokens": 400, "overlap_tokens": 40, **overrides})


def index_with(*entries: MemoryEntry) -> SpaceIndex:
    index = SpaceIndex(space=SPACE)
    for item in entries:
        index.add(item, spec())
    return index


# --- 임베더 -------------------------------------------------------------------


def test_hash_embedder_is_deterministic():
    """같은 입력은 항상 같은 벡터 — 테스트가 비결정성에 의존하지 않는다."""
    embedder = HashEmbedder()

    first = embedder.embed(["mcp transport 재연결"])
    second = embedder.embed(["mcp transport 재연결"])

    assert first == second


def test_embedder_respects_the_declared_dimensions():
    embedder = HashEmbedder(dimensions=32)

    (vector,) = embedder.embed(["hello world"])

    assert len(vector) == 32


def test_identical_text_is_maximally_similar():
    embedder = HashEmbedder()

    (a,), (b,) = embedder.embed(["같은 문장"]), embedder.embed(["같은 문장"])

    assert cosine(a, b) == pytest.approx(1.0)


def test_shared_tokens_bring_vectors_closer():
    """토큰을 공유하는 문장이 전혀 다른 문장보다 가까워야 한다."""
    embedder = HashEmbedder()
    (base,) = embedder.embed(["mcp transport reconnect failure"])
    (near,) = embedder.embed(["mcp transport reconnect"])
    (far,) = embedder.embed(["completely unrelated wording here"])

    assert cosine(base, near) > cosine(base, far)


def test_empty_vector_normalizes_without_dividing_by_zero():
    assert normalize([0.0, 0.0]) == (0.0, 0.0)


def test_mismatched_dimensions_do_not_compare():
    """혼합된 임베딩 공간을 조용히 비교하지 않는다."""
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mcp__fs__read_file", ("mcp__fs__read_file",)),
        ("MCP_004 발생", ("mcp_004", "발생")),
        ("a, b; c", ("a", "b", "c")),
        ("", ()),
    ],
)
def test_tokenize_keeps_identifiers_intact(text, expected):
    """식별자를 잘게 쪼개면 lexical 검색이 그것을 놓친다."""
    assert tokenize(text) == expected


# --- chunking -----------------------------------------------------------------


def test_short_content_is_one_chunk():
    assert split_chunks("짧은 내용", spec()) == ("짧은 내용",)


def test_long_content_is_split_with_overlap():
    """겹침이 없으면 경계에 걸친 문장이 양쪽 어디서도 검색되지 않는다."""
    text = " ".join(f"w{i}" for i in range(25))

    chunks = split_chunks(text, spec(max_tokens=10, overlap_tokens=3))

    assert len(chunks) > 1
    first_tail = chunks[0].split()[-3:]
    second_head = chunks[1].split()[:3]
    assert first_tail == second_head


def test_chunks_cover_the_whole_text():
    text = " ".join(f"w{i}" for i in range(25))

    chunks = split_chunks(text, spec(max_tokens=10, overlap_tokens=3))

    covered = {token for chunk in chunks for token in chunk.split()}
    assert covered == set(text.split())


# --- 세 계층 검색 --------------------------------------------------------------


def test_vector_search_matches_related_wording():
    """표현이 달라도 토큰을 공유하면 잡힌다."""
    related = entry("mcp transport 단절 시 재연결 backoff")
    index = index_with(related, entry("완전히 무관한 문서 내용"))

    hits = index.search_vector("mcp transport 재연결", k=5)

    assert hits[0].entry_id == related.entry_id


def test_lexical_search_finds_an_exact_identifier():
    """에러 코드 같은 식별자는 vector 만으로 놓친다 — lexical 이 담당한다."""
    target = entry("재연결 실패는 MCP_004 로 보고된다")
    index = index_with(target, entry("일반적인 설명 문서"))

    hits = index.search_lexical("MCP_004", k=5)

    assert [h.entry_id for h in hits] == [target.entry_id]


def test_lexical_search_prefers_rare_tokens():
    """흔한 토큰이 이기면 식별자 검색이 묻힌다."""
    rare = entry("문서 문서 문서 mcp__fs__read_file")
    for_noise = [entry("문서 문서 문서 일반") for _ in range(3)]
    index = index_with(rare, *for_noise)

    hits = index.search_lexical("문서 mcp__fs__read_file", k=5)

    assert hits[0].entry_id == rare.entry_id


def test_lexical_search_without_matching_tokens_returns_nothing():
    index = index_with(entry("아무 내용"))

    assert index.search_lexical("존재하지않는토큰", k=5) == ()


def test_empty_query_returns_nothing():
    index = index_with(entry("아무 내용"))

    assert index.search_lexical("", k=5) == ()


def test_metadata_filter_narrows_by_kind():
    fact = entry("사실 문서", kind=MemoryKind.FACT)
    observation = entry("관찰 문서", kind=MemoryKind.OBSERVATION)
    index = index_with(fact, observation)

    hits = index.search_lexical("문서", k=5, kinds=[MemoryKind.FACT])

    assert [h.entry_id for h in hits] == [fact.entry_id]


def test_metadata_filter_requires_all_tags():
    both = entry("문서 하나", tags=("mcp", "sidecar"))
    one = entry("문서 둘", tags=("mcp",))
    index = index_with(both, one)

    hits = index.search_lexical("문서", k=5, tags=["mcp", "sidecar"])

    assert [h.entry_id for h in hits] == [both.entry_id]


def test_results_are_deduplicated_per_entry():
    """같은 기억이 여러 번 주입되면 예산만 낭비된다."""
    long_text = " ".join(["mcp"] * 60)
    index = SpaceIndex(space=SPACE)
    index.add(entry(long_text), spec(max_tokens=10, overlap_tokens=2))

    hits = index.search_lexical("mcp", k=10)

    assert len(hits) == 1


def test_ranks_start_at_one_and_increase():
    index = index_with(entry("alpha beta"), entry("beta gamma"))

    hits = index.search_lexical("beta", k=5)

    assert [h.rank for h in hits] == [1, 2]


# --- space 격리 ----------------------------------------------------------------


def test_index_rejects_entries_from_another_space():
    """인덱스가 space 경계를 넘으면 격리가 무너진다."""
    index = SpaceIndex(space=SPACE)

    with pytest.raises(MalkuthError) as exc_info:
        index.add(entry("남의 기억", space="local:writer:longterm"), spec())

    assert exc_info.value.code == "MEM_004"


def test_searches_never_see_another_space():
    registry = IndexRegistry()
    mine = entry("공유되지 않아야 할 내용")
    theirs = entry("공유되지 않아야 할 내용", space="local:writer:longterm")
    registry.submit(mine, spec())
    registry.submit(theirs, spec())
    registry.drain()

    hits = registry.index_for(SPACE).search_lexical("내용", k=10)

    assert [h.entry_id for h in hits] == [mine.entry_id]


def test_removing_an_entry_clears_its_chunks():
    target = entry("삭제 대상")
    index = index_with(target, entry("남을 항목"))

    index.remove(target.entry_id)

    assert target.entry_id not in index.entry_ids
    assert all(c.entry_id != target.entry_id for c in index.chunks)


# --- 비동기 인덱싱 / eventual consistency ---------------------------------------


def test_submitted_entries_are_not_searchable_until_drained():
    """eventual consistency 가 계약이다 — 방금 저장한 항목은 즉시 안 나올 수 있다."""
    registry = IndexRegistry()
    registry.submit(entry("아직 색인되지 않음"), spec())

    assert registry.index_for(SPACE).search_lexical("색인", k=5) == ()
    assert registry.queue.lag == 1


def test_draining_makes_entries_searchable():
    registry = IndexRegistry()
    registry.submit(entry("색인 후 검색"), spec())

    indexed = registry.drain()

    assert indexed == 1
    assert registry.queue.lag == 0
    assert registry.index_for(SPACE).search_lexical("검색", k=5)


def test_search_results_are_stable_while_indexing_lags():
    """색인 지연 중에도 이미 색인된 결과는 흔들리지 않는다."""
    registry = IndexRegistry()
    first = entry("첫 번째 문서")
    registry.submit(first, spec())
    registry.drain()

    registry.submit(entry("두 번째 문서"), spec())  # 아직 drain 하지 않는다
    hits = registry.index_for(SPACE).search_lexical("문서", k=5)

    assert [h.entry_id for h in hits] == [first.entry_id]


def test_failed_indexing_is_retried():
    queue = IndexQueue()
    stray = entry("다른 space", space="local:writer:longterm")
    queue.submit(stray, spec())

    indexed = queue.drain({SPACE: SpaceIndex(space=SPACE)})

    assert indexed == 0
    assert queue.lag == 1  # 재시도를 위해 남는다


def test_repeated_indexing_failure_is_mem_003():
    """조용히 색인되지 않은 기억은 검색에서 영원히 사라진다."""
    queue = IndexQueue(max_failures=2)
    stray = entry("다른 space", space="local:writer:longterm")
    indexes = {SPACE: SpaceIndex(space=SPACE)}

    queue.submit(stray, spec())
    queue.drain(indexes)

    queue.submit(stray, spec())
    with pytest.raises(MalkuthError) as exc_info:
        queue.drain(indexes)

    assert exc_info.value.code == "MEM_003"


def test_success_clears_the_failure_count():
    queue = IndexQueue(max_failures=2)
    item = entry("일시적 실패 후 성공")
    queue.submit(item, spec())
    queue.drain({})  # space 인덱스가 없어 실패

    queue.drain({SPACE: SpaceIndex(space=SPACE)})

    assert queue.failures == {}


# --- 재인덱싱 -----------------------------------------------------------------


def test_reindex_rebuilds_with_the_new_embedder():
    registry = IndexRegistry()
    item = entry("재인덱싱 대상 문서")
    registry.submit(item, spec())
    registry.drain()

    rebuilt = registry.reindex(SPACE, [item], spec(), embedder=HashEmbedder(dimensions=16))

    assert rebuilt.entry_ids == {item.entry_id}
    assert len(rebuilt.chunks[0].vector) == 16


def test_old_index_serves_searches_until_the_swap():
    """절반만 채워진 인덱스로 검색하면 있는 기억이 없다고 나온다."""
    registry = IndexRegistry()
    item = entry("기존 문서")
    registry.submit(item, spec())
    registry.drain()
    old = registry.index_for(SPACE)

    # 교체 전에는 구 인덱스가 그대로 응답한다
    assert old.search_lexical("문서", k=5)

    registry.reindex(SPACE, [item], spec(), embedder=HashEmbedder(dimensions=16))

    assert registry.index_for(SPACE) is not old
    assert registry.index_for(SPACE).search_lexical("문서", k=5)


def test_reindex_replaces_the_registry_embedder():
    """이후 색인이 옛 차원으로 섞이면 혼합 임베딩 공간이 된다."""
    registry = IndexRegistry()
    registry.reindex(SPACE, [], spec(), embedder=HashEmbedder(dimensions=16))

    assert registry.embedder.dimensions == 16


def test_entry_rejected_by_the_index_is_retried_not_lost():
    """space 인덱스는 있는데 항목이 거부되는 경우도 조용히 버리지 않는다."""
    queue = IndexQueue(max_failures=5)
    # SPACE 인덱스는 존재하지만 이 항목은 다른 space 소속이다
    indexes = {"local:writer:longterm": SpaceIndex(space=SPACE)}
    stray = entry("space 가 어긋난 항목", space="local:writer:longterm")
    queue.submit(stray, spec())

    indexed = queue.drain(indexes)

    assert indexed == 0
    assert queue.lag == 1
    assert queue.failures[stray.entry_id] == 1
