"""Control Plane client for the CLI.

CLI 가 다른 프로세스의 run 을 조작하는 창구 (#102). 연결 실패를 그대로
노출하지 않는다 — 운영자가 보는 것은 ``ConnectionRefusedError`` 가 아니라
"어디에 무엇을 기대했는데 닿지 않았다" 여야 한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_CONTROL_URL = "http://127.0.0.1:8700"
DEFAULT_TIMEOUT_S = 10.0


def unreachable(url: str, err: Exception) -> MalkuthError:
    """Control Plane 에 닿지 못함 — 연결 거부를 그대로 보여주지 않는다."""
    return MalkuthError(
        category=ErrorCategory.NETWORK,
        code=ErrorCode.NET_001,
        message=f"control plane is unreachable at {url}",
        retryable=True,
        details={"url": url, "cause": type(err).__name__},
    )


def _failed(url: str, response: httpx.Response, *, run_scoped: bool) -> MalkuthError:
    """서비스가 돌려준 실패를 구조화 에러로.

    404 를 무조건 "unknown run" 으로 읽지 않는다: 목록 조회에는 run id 가
    없으므로, 그 404 는 **엔드포인트가 없다**는 뜻이다 (버전이 안 맞는 Control
    Plane). 둘을 뭉개면 운영자가 없는 run 을 찾아 헤맨다.
    """
    if response.status_code == httpx.codes.NOT_FOUND and run_scoped:
        return MalkuthError(
            category=ErrorCategory.NOT_FOUND,
            code=ErrorCode.NF_001,
            message="unknown run",
            details={"url": url, "status": str(response.status_code)},
        )
    return MalkuthError(
        category=ErrorCategory.RUNTIME,
        code=ErrorCode.GRAPH_001,
        message=_detail(response),
        details={"url": url, "status": str(response.status_code)},
    )


def _detail(response: httpx.Response) -> str:
    """응답 본문에서 사람이 읽을 사유를 꺼낸다."""
    try:
        body = response.json()
    except ValueError:
        return response.text or "control plane request failed"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return "control plane request failed"


class ControlClient:
    """Talks to the Control Plane over HTTP.

    Control Plane 과 대화하는 클라이언트.
    """

    def __init__(
        self, base_url: str = DEFAULT_CONTROL_URL, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _request(self, method: str, path: str, *, run_scoped: bool = True) -> Any:
        """요청 한 건 — 연결 실패와 서비스 실패를 나눠 옮긴다.

        Args:
            method: HTTP method.
            path: Request path.
            run_scoped: Whether a 404 means "that run does not exist" — 목록
                조회는 False 다 (그 404 는 엔드포인트 부재를 뜻한다).
        """
        url = f"{self._base}{path}"
        try:
            response = httpx.request(method, url, timeout=self._timeout_s)
        except httpx.HTTPError as err:
            raise unreachable(self._base, err) from err

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise _failed(url, response, run_scoped=run_scoped)
        return response.json()

    def list_runs(self, *, mode: str | None = None) -> Sequence[dict[str, Any]]:
        """진행 중 run 목록 — mode 로 좁힐 수 있다."""
        query = f"?mode={mode}" if mode else ""
        listed: list[dict[str, Any]] = self._request("GET", f"/v1/runs{query}", run_scoped=False)
        return listed

    def get_run(self, run_id: str) -> dict[str, Any]:
        """run 하나의 상태."""
        found: dict[str, Any] = self._request("GET", f"/v1/runs/{run_id}")
        return found

    def drain(self, run_id: str) -> dict[str, Any]:
        """drain 을 요청한다 — 정지는 구동 프로세스가 iteration 경계에서 한다."""
        result: dict[str, Any] = self._request("POST", f"/v1/runs/{run_id}/drain")
        return result

    def resume(self, run_id: str) -> dict[str, Any]:
        """halted run 을 재개한다 — 구동 프로세스만 응답할 수 있다."""
        result: dict[str, Any] = self._request("POST", f"/v1/runs/{run_id}/resume")
        return result


__all__ = [
    "DEFAULT_CONTROL_URL",
    "ControlClient",
    "unreachable",
]
