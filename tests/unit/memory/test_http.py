"""Memory Service HTTP surface and its client.

에이전트는 HTTP 로만 메모리에 닿는다 — **컨테이너에 DB 자격증명을 주지 않기
위해서다** (09 Access Enforcement 1). 여기서는 그 왕복과 경계를 검증한다.
"""

from __future__ import annotations

import httpx
import pytest

from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.memory.entry import MemoryEntry, MemorySource
from malkuth.memory.http import TokenRegistry, create_app
from malkuth.memory.index import SpaceIndex
from malkuth.memory.recall import Recall
from malkuth.memory.service import MemoryService, build_token
from malkuth.memory.store import SqliteMemoryStore
from malkuth.modules.memoryset import ChunkSpec, MemoryKind
from malkuth.runtime.memory_http import HttpMemoryAccess

BASE_URL = "http://memory.test"


@pytest.fixture
def served():
    """서비스 앱과 그것을 통해 말하는 클라이언트."""
    store = SqliteMemoryStore()
    service = MemoryService(store=store)
    token = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])
    space_id = token.resolve("longterm").space_id  # type: ignore[union-attr]

    indexes = {space_id: SpaceIndex(space=space_id)}
    tokens = TokenRegistry()
    secret = tokens.issue(token)
    app = create_app(service, Recall(indexes=indexes), tokens)

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    access = HttpMemoryAccess(base_url=BASE_URL, token=secret, client=client)
    try:
        yield access, service, token, space_id, indexes, tokens
    finally:
        store.close()


def entry(space: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        space=space,
        kind=MemoryKind.FACT,
        content=content,
        source=MemorySource(agent="researcher"),
    )


# --- 왕복 --------------------------------------------------------------------


async def test_append_then_search_round_trips(served):
    access, _service, _token, space_id, indexes, _tokens = served
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")

    stored = await access.append("longterm", entry=item)
    indexes[space_id].add(stored, ChunkSpec(max_tokens=400, overlap_tokens=40))
    found = await access.search("sidecar")

    assert [scored.entry.entry_id for scored in found] == [item.entry_id]
    assert found[0].space == space_id


async def test_spaces_reports_what_this_token_reaches(served):
    access, _service, _token, _space_id, _indexes, _tokens = served

    listed = await access.spaces()

    assert [space["alias"] for space in listed] == ["longterm"]


# --- 경계 --------------------------------------------------------------------


async def test_undeclared_space_is_denied(served):
    """선언되지 않은 space 는 서비스가 거부한다 — 클라이언트가 판단하지 않는다."""
    access, *_ = served

    with pytest.raises(MalkuthError) as exc_info:
        await access.search("무엇이든", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001
    assert not exc_info.value.retryable


async def test_unknown_token_is_denied(served):
    """토큰을 위조할 수 없다 — 서비스가 발급분만 인정한다."""
    access, *_ = served
    forged = HttpMemoryAccess(base_url=BASE_URL, token="forged", client=access.client)

    with pytest.raises(MalkuthError) as exc_info:
        await forged.spaces()

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_missing_token_is_denied(served):
    access, *_ = served
    anonymous = HttpMemoryAccess(base_url=BASE_URL, token="", client=access.client)

    with pytest.raises(MalkuthError) as exc_info:
        await anonymous.spaces()

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_forgotten_token_stops_working(served):
    """그룹 이동이나 재배포 시 즉시 무효화된다."""
    access, _service, _token, _space_id, _indexes, tokens = served
    tokens.forget(access.token)

    with pytest.raises(MalkuthError):
        await access.spaces()


# --- 장애와 거부의 구분 ----------------------------------------------------------


async def test_unreachable_service_is_retryable():
    """거부와 장애를 같게 다루면 재시도 판단이 어긋난다."""

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url=BASE_URL)
    access = HttpMemoryAccess(base_url=BASE_URL, token="t", client=client)

    with pytest.raises(MalkuthError) as exc_info:
        await access.search("q")

    assert exc_info.value.code == ErrorCode.MEM_004
    assert exc_info.value.retryable


async def test_server_error_is_retryable():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "index rebuilding"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail), base_url=BASE_URL)
    access = HttpMemoryAccess(base_url=BASE_URL, token="t", client=client)

    with pytest.raises(MalkuthError) as exc_info:
        await access.search("q")

    assert exc_info.value.retryable
