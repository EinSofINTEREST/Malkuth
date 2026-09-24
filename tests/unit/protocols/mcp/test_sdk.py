"""MCP SDK binding — pure conversion logic.

세션 수립·호출·정리는 **실제 서버로** 검증한다
(``tests/integration/protocols/test_mcp_stdio_session.py``). 대역으로 흉내내면
anyio cancel scope 같은 실제 제약을 놓친다 — 실제로 그것 때문에 다른 태스크의
정리가 멈추는 결함이 있었다.

여기서는 서버가 필요 없는 변환만 본다.
"""

from __future__ import annotations

from typing import get_args, get_type_hints

import httpx2
import pytest

from malkuth.protocols.mcp.sdk import _open_streamable_http, _render_block, _terminate
from malkuth.protocols.mcp.session import Connection


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class StructuredBlock:
    """text 가 없는 블록 — 이미지·리소스 등."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "python") -> dict:
        return self._payload


def test_text_blocks_render_as_text():
    assert _render_block(TextBlock("file contents")) == "file contents"


def test_non_text_blocks_fall_back_to_their_payload():
    """text 가 없다고 버리면 이미지·리소스 결과가 조용히 사라진다."""
    payload = {"type": "image", "data": "..."}

    assert _render_block(StructuredBlock(payload)) == payload


async def test_terminating_an_unopened_connection_is_safe():
    """기동 실패 경로에서도 정리가 불려온다 — 핸들이 없으면 조용히 끝난다."""
    await _terminate(Connection(tools=(), protocol_version="", handle=None))


@pytest.mark.parametrize("text", ["", "여러 줄\n내용"])
def test_empty_and_multiline_text_survive(text):
    assert _render_block(TextBlock(text)) == text


# --- streamable-http 전송의 클라이언트 계약 (#317) --------------------------------------


def test_the_sdk_still_takes_an_httpx2_client():
    """SDK 가 받는 클라이언트 타입이 바뀌면 인증 헤더가 조용히 빠진다 — 업그레이드 때 깨진다."""
    from mcp.client.streamable_http import streamable_http_client

    hint = get_type_hints(streamable_http_client)["http_client"]

    assert httpx2.AsyncClient in get_args(hint)


async def test_auth_headers_ride_on_the_client_the_sdk_receives(monkeypatch):
    import mcp.client.streamable_http as transport

    received = {}

    def fake_client(url, *, http_client):
        received.update(url=url, client=http_client)
        return "opened"

    monkeypatch.setattr(transport, "streamable_http_client", fake_client)

    opened = _open_streamable_http(url="http://proxy/mcp", headers={"Authorization": "Bearer t"})

    client = received["client"]
    try:
        assert opened == "opened"
        assert isinstance(client, httpx2.AsyncClient)
        assert client.headers["authorization"] == "Bearer t"
    finally:
        await client.aclose()
