"""Auto-recall through a real embedding binding.

#159 가 provider 를 바인딩했지만 대역 endpoint 없이는 그 경로가 유닛에서만
돌았다. 여기서는 **실제 HTTP 왕복**으로 축적 → 회상을 태운다 (#161).

06 은 실 embedding API 호출을 금지한다 — endpoint 는 E2E 스택이 쓰는 것과
같은 대역이다.
"""

from __future__ import annotations

import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from malkuth.memory.entry import MemoryEntry, MemoryKind, MemorySource
from malkuth.memory.providers import BASE_URL_ENV, build_index_registry
from malkuth.modules.memoryset import ChunkSpec, EmbeddingSpec

pytestmark = pytest.mark.integration

DIMENSIONS = 64
SPACE = "longterm"

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deployments/docker/fake-provider"))


@pytest.fixture
def embedding_endpoint(monkeypatch):
    """E2E 스택이 쓰는 것과 **같은 대역**을 띄운다.

    별도 대역을 쓰면 스택과 갈라져, 여기서 통과한 것이 컨테이너에서 깨진다.
    """
    import server as fake

    httpd = HTTPServer(("127.0.0.1", 0), fake.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(BASE_URL_ENV, f"http://127.0.0.1:{httpd.server_address[1]}")
        yield
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def spec() -> EmbeddingSpec:
    return EmbeddingSpec(
        provider="openai-compatible", model="text-embedding-3-small", dimensions=DIMENSIONS
    )


def remembered(content: str) -> MemoryEntry:
    return MemoryEntry(
        space=SPACE,
        kind=MemoryKind.FACT,
        content=content,
        source=MemorySource(agent="researcher"),
    )


def test_the_binding_is_used_not_the_offline_default(embedding_endpoint):
    """대역이 있는데도 HashEmbedder 로 떨어지면 이 경로가 검증되지 않는다."""
    from malkuth.memory.providers import OpenAICompatibleEmbedder

    registry = build_index_registry(spec())

    assert isinstance(registry.embedder, OpenAICompatibleEmbedder)


def test_a_stored_memory_is_found_by_a_later_search(embedding_endpoint):
    """축적 → 회상이 **실제 HTTP 경로**로 이어지는지 (#161 의 핵심)."""
    registry = build_index_registry(spec())
    chunking = ChunkSpec(max_tokens=400, overlap_tokens=40)

    wanted = remembered("mcp sidecar 는 이미지 태그 고정이 필요하다")
    registry.submit(wanted, chunking)
    registry.submit(remembered("전혀 관계없는 다른 사실"), chunking)
    # 09 Write Path — append 는 색인을 기다리지 않는다. 기다리지 않고 검색하면
    # 간헐 실패한다
    indexed = registry.drain()

    # drain 은 실패를 **큐에 남기고** 상한 전까지 조용히 넘긴다 — 색인 건수를
    # 확인하지 않으면 embedding 이 전부 실패해도 이 테스트가 통과한다
    assert indexed == 2

    hits = registry.index_for(SPACE).search_vector("mcp sidecar 태그", k=1)

    assert hits
    assert hits[0].entry_id == wanted.entry_id


def test_indexing_is_deterministic_across_registries(embedding_endpoint):
    """같은 입력이 늘 같은 벡터라야 검색 결과가 재현된다."""
    chunking = ChunkSpec(max_tokens=400, overlap_tokens=40)
    entries = [
        remembered("mcp sidecar 는 이미지 태그 고정이 필요하다"),
        remembered("전혀 관계없는 다른 사실"),
    ]
    found = []
    for _ in range(2):
        registry = build_index_registry(spec())
        for entry in entries:
            registry.submit(entry, chunking)
        assert registry.drain() == len(entries)
        hits = registry.index_for(SPACE).search_vector("mcp sidecar 태그", k=2)
        found.append([hit.entry_id for hit in hits])

    assert found[0] == found[1]


def test_a_dimension_mismatch_is_refused(embedding_endpoint):
    """차원이 어긋난 벡터를 섞으면 같은 인덱스 안에 두 공간이 생긴다 (09)."""
    from malkuth.core.errors import ErrorCode, MalkuthError

    wrong = EmbeddingSpec(provider="openai-compatible", model="m", dimensions=DIMENSIONS + 8)
    registry = build_index_registry(wrong)

    with pytest.raises(MalkuthError) as exc_info:
        registry.index_for(SPACE).add(remembered("x"), ChunkSpec(max_tokens=400, overlap_tokens=40))

    assert exc_info.value.code == ErrorCode.MEM_003
