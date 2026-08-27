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
    """Derive a deterministic response from the prompt.

    프롬프트로부터 결정적 응답을 만듭니다 — 해시를 써서 같은 입력이 항상 같은
    출력을 내되, 서로 다른 입력은 구분됩니다.
    """
    digest = hashlib.blake2b(prompt.encode("utf-8"), digest_size=6).hexdigest()
    return {
        "content": _content_for(prompt, digest),
        "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 8},
    }


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

        body = json.dumps(respond_to(str(payload.get("prompt", "")))).encode()
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
