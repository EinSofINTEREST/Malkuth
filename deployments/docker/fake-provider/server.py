"""Deterministic fake model provider for E2E.

E2E 에서도 실제 LLM 을 호출하지 않는다 (06 Testing 3) — 비결정적이고 비용이
들며, CI 에서 API key 부재로 실패해야 정상이다.

이 서버는 요청 내용으로 응답을 결정한다: 같은 입력이면 항상 같은 출력이라
테스트가 흔들리지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

PORT = int(os.environ.get("FAKE_PROVIDER_PORT", "8000"))
MAX_BODY_BYTES = 1 << 20


def respond_to(prompt: str) -> dict[str, Any]:
    """Derive a deterministic Messages API response from the prompt.

    프롬프트로부터 결정적 응답을 만듭니다 — 해시를 써서 같은 입력이 항상 같은
    출력을 내되, 서로 다른 입력은 구분됩니다.

    **Anthropic Messages API 의 응답 모양**을 따릅니다. 자체 형식으로 답하면
    에이전트가 이 대역을 쓸 수 없어, E2E 가 실제 실행 경로를 태우지 못합니다
    (#153).
    """
    digest = hashlib.blake2b(prompt.encode("utf-8"), digest_size=6).hexdigest()
    return {
        "id": f"msg_{digest}",
        "type": "message",
        "role": "assistant",
        "model": "fake-model",
        "content": [{"type": "text", "text": _content_for(prompt, digest)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": max(len(prompt) // 4, 1), "output_tokens": 8},
    }


def prompt_of(payload: dict[str, Any]) -> str:
    """요청에서 프롬프트를 꺼낸다 — Messages API 는 ``messages[]`` 로 싣는다."""
    parts: list[str] = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(block.get("text", "") for block in content if isinstance(block, dict))
    return "\n".join(parts)


_ASKS_FOR = re.compile(r"exactly these keys: (.+)")


def _content_for(prompt: str, digest: str) -> str:
    """프롬프트가 요구한 형태로 응답한다.

    출력 키를 요구하는 프롬프트에는 **그 키를 담은 JSON** 으로 답한다 —
    에이전트 이름을 하드코딩하지 않고 프롬프트에서 읽으므로, 새 에이전트가
    늘어도 이 대역을 고치지 않는다.
    """
    # **마지막** 매치를 쓴다: 계약은 템플릿 끝에 붙고, 그 앞의 렌더된 사용자
    # 입력에 같은 문구가 섞이면 대역이 입력에 휘둘린다
    matches = _ASKS_FOR.findall(prompt)
    if not matches:
        return f"fake-response:{digest}"

    keys = [key.strip() for key in matches[-1].split(",") if key.strip()]
    return json.dumps({key: f"fake-{key}:{digest}" for key in keys})


def _embedding_dimensions() -> int:
    """대역 벡터 차원 — memoryset 선언과 맞춰야 한다 (#159 가 불일치를 거부한다).

    0 이하를 그대로 두면 벡터를 만들 때 modulo 0 으로 죽는다 — 요청마다
    터지느니 기동 시점에 거부한다.
    """
    declared = os.environ.get("FAKE_EMBEDDING_DIMENSIONS", "64")
    try:
        value = int(declared)
    except ValueError as err:
        raise ValueError(f"FAKE_EMBEDDING_DIMENSIONS must be an integer: {declared!r}") from err
    if value <= 0:
        raise ValueError(f"FAKE_EMBEDDING_DIMENSIONS must be positive: {value}")
    return value


EMBEDDING_DIMENSIONS = _embedding_dimensions()


def embed(payload: dict[str, Any]) -> dict[str, Any]:
    """Answer an OpenAI-compatible embeddings request.

    입력마다 결정적 벡터를 돌려줍니다 — 같은 텍스트는 늘 같은 벡터라
    검색 결과가 재현됩니다.

    의미 유사도를 흉내내지는 않지만, **HTTP 경로·직렬화·차원 검증**이 실제로
    실행되는 것이 이 대역의 목적입니다.
    """
    texts = payload.get("input") or []
    if isinstance(texts, str):
        texts = [texts]
    return {
        "object": "list",
        "model": payload.get("model", "fake-embedding"),
        "data": [
            {"object": "embedding", "index": index, "embedding": _vector(str(text))}
            for index, text in enumerate(texts)
        ],
        "usage": {"prompt_tokens": sum(len(str(t)) // 4 for t in texts), "total_tokens": 0},
    }


def _vector(text: str) -> list[float]:
    """토큰 해시를 차원에 누적한 뒤 정규화한다 — 같은 토큰이면 가까워진다."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in text.lower().split():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = sum(value * value for value in vector) ** 0.5
    return [value / norm for value in vector] if norm else vector


class Handler(BaseHTTPRequestHandler):
    """Minimal request handler — one endpoint, no auth."""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 규약
        """모델 호출 요청에 결정적으로 응답한다."""
        length = min(int(self.headers.get("content-length", 0)), MAX_BODY_BYTES)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return

        # 경로로 갈라야 한다 — 모델과 embedding 은 응답 모양이 다르다
        answer = (
            embed(payload)
            if self.path.rstrip("/").endswith("/embeddings")
            else respond_to(prompt_of(payload))
        )
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """헬스 확인 — compose 의 healthcheck 가 호출한다."""
        body = b'{"status":"healthy"}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """기본 stderr 로그를 끈다 — 테스트 출력을 어지럽힌다."""


def main() -> None:
    """Serve until stopped."""
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
