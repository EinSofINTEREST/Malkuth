"""Agent Control API client.

agentd 가 컨테이너 내부에서 서빙하는 표준 API 의 클라이언트. 오케스트레이터가
에이전트를 호출하는 유일한 통로이며, 이 계층 밖에서 에이전트를 직접 호출하지 않는다.

원격 호출 실패는 전부 ``MalkuthError`` 로 변환한다 — 재시도/서킷브레이커 판단이
카테고리와 코드에 의존하기 때문이다.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from malkuth.core.agent import HealthStatus, TaskRequest, TaskResult
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.events import TaskEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic import TypeAdapter

DEFAULT_CONTROL_PORT = 8080
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_HEALTH_TIMEOUT_S = 3.0

_SSE_DATA_PREFIX = "data:"


def _event_adapter() -> TypeAdapter[TaskEvent]:
    """TaskEvent 판별 유니온 어댑터 — 모듈 로드 시 부수효과를 만들지 않으려 지연 생성."""
    from pydantic import TypeAdapter

    return TypeAdapter(TaskEvent)


class ControlClient:
    """HTTP client for one agent's Control API.

    에이전트 하나의 Control API 클라이언트. runtime 이 발급한 per-agent token 을
    모든 인증 필요 요청에 싣는다.
    """

    def __init__(
        self,
        base_url: str,
        *,
        agent: str,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent = agent
        self._token = token
        self._timeout_s = timeout_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    @property
    def agent(self) -> str:
        """대상 에이전트 이름."""
        return self._agent

    async def aclose(self) -> None:
        """Release the underlying transport when this client owns it.

        직접 만든 transport 만 정리합니다 (주입받은 것은 소유자가 닫습니다).
        """
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ControlClient:
        """비동기 컨텍스트 진입."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """컨텍스트 종료 시 transport 정리."""
        await self.aclose()

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        """요청 헤더 — 토큰은 인증이 필요한 경로에만 싣는다."""
        headers = {"content-type": "application/json"}
        if authenticated and self._token is not None:
            headers["authorization"] = f"Bearer {self._token}"
        return headers

    async def invoke(self, task: TaskRequest, *, timeout_s: float | None = None) -> TaskResult:
        """Run a task synchronously.

        태스크를 동기 실행합니다 (202+polling 이 아닌 동기 응답).

        Args:
            task: The task to run.
            timeout_s: Per-call timeout; falls back to ``task.config.timeout_s``.

        Returns:
            The task result.

        Raises:
            MalkuthError: On transport, timeout, or non-2xx responses.
        """
        payload = await self._post_json(
            "/v1/invoke",
            body=task.model_dump(mode="json"),
            timeout_s=timeout_s or task.config.timeout_s,
            task_id=task.task_id,
        )
        return TaskResult.model_validate(payload)

    async def stream(
        self, task: TaskRequest, *, timeout_s: float | None = None
    ) -> AsyncIterator[TaskEvent]:
        """Stream task events over SSE.

        태스크 이벤트를 SSE 로 소비합니다 — 장시간 태스크는 이 경로를 씁니다.

        Args:
            task: The task to run.
            timeout_s: Per-call timeout; falls back to the task config.

        Yields:
            Task events in emission order.

        Raises:
            MalkuthError: On transport, timeout, or non-2xx responses.
        """
        adapter = _event_adapter()
        url = f"{self._base_url}/v1/stream"

        try:
            async with self._client.stream(
                "POST",
                url,
                json=task.model_dump(mode="json"),
                headers=self._headers(),
                timeout=timeout_s or task.config.timeout_s,
            ) as response:
                self._raise_for_status(response, task_id=task.task_id)
                async for line in response.aiter_lines():
                    event = self._parse_sse_line(line, adapter)
                    if event is not None:
                        yield event
        except httpx.TimeoutException as err:
            raise self._timeout_error(err, task_id=task.task_id) from err
        except httpx.TransportError as err:
            raise self._transport_error(err, task_id=task.task_id) from err

    @staticmethod
    def _parse_sse_line(line: str, adapter: TypeAdapter[TaskEvent]) -> TaskEvent | None:
        """SSE 한 줄에서 이벤트를 파싱한다 — 주석/빈 줄은 건너뛴다."""
        if not line.startswith(_SSE_DATA_PREFIX):
            return None
        raw = line[len(_SSE_DATA_PREFIX) :].strip()
        if not raw or raw == "[DONE]":
            return None
        return adapter.validate_python(json.loads(raw))

    async def health(self) -> HealthStatus:
        """Read aggregated agent health.

        에이전트 상태를 조회합니다. ``/health`` 는 무인증 경로입니다
        (Docker healthcheck 가 직접 호출).
        """
        payload = await self._request_json(
            "GET",
            "/v1/health",
            timeout_s=DEFAULT_HEALTH_TIMEOUT_S,
            authenticated=False,
        )
        return HealthStatus.model_validate(payload)

    async def card(self) -> dict[str, Any]:
        """Read the agent's A2A card.

        A2A AgentCard 를 조회합니다.
        """
        payload: dict[str, Any] = await self._request_json("GET", "/v1/card")
        return payload

    async def cancel(self, task_id: str) -> None:
        """Cancel an in-flight task.

        진행 중 태스크를 취소합니다.
        """
        await self._request_json("POST", f"/v1/cancel/{task_id}", task_id=task_id)

    async def reload(self) -> None:
        """Hot-reload promptset and skillset modules.

        promptset/skillset 을 무중단 리로드합니다 (신규 태스크부터 적용).
        """
        await self._request_json("POST", "/v1/reload")

    async def drain(self) -> None:
        """Begin graceful drain.

        새 태스크 수락을 중지하고 진행 중 태스크를 마치도록 요청합니다.
        """
        await self._request_json("POST", "/v1/drain")

    async def _post_json(
        self,
        path: str,
        *,
        body: dict[str, Any],
        timeout_s: float,
        task_id: str | None = None,
    ) -> Any:
        """JSON 본문을 실어 POST 한다."""
        return await self._request_json(
            "POST", path, body=body, timeout_s=timeout_s, task_id=task_id
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        task_id: str | None = None,
        authenticated: bool = True,
    ) -> Any:
        """Control API 를 호출하고 실패를 구조화 에러로 변환한다."""
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.request(
                method,
                url,
                json=body,
                headers=self._headers(authenticated=authenticated),
                timeout=timeout_s or self._timeout_s,
            )
        except httpx.TimeoutException as err:
            raise self._timeout_error(err, task_id=task_id) from err
        except httpx.TransportError as err:
            raise self._transport_error(err, task_id=task_id) from err

        self._raise_for_status(response, task_id=task_id)

        if not response.content:
            return {}
        return response.json()

    def _raise_for_status(self, response: httpx.Response, *, task_id: str | None) -> None:
        """비 2xx 응답을 구조화 에러로 변환한다."""
        if response.is_success:
            return

        status = response.status_code
        if status == 404:
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message=f"control endpoint not found: {response.request.url.path}",
                agent=self._agent,
                task_id=task_id,
            )
        if status in (401, 403):
            raise MalkuthError(
                category=ErrorCategory.FORBIDDEN,
                code=ErrorCode.RT_002,
                message="control api rejected the agent token",
                agent=self._agent,
                task_id=task_id,
            )

        raise MalkuthError(
            category=ErrorCategory.RUNTIME,
            code=ErrorCode.RT_002,
            message=f"control api returned status {status}",
            agent=self._agent,
            task_id=task_id,
            # 5xx 는 일시적일 수 있으므로 재시도 대상
            retryable=status >= 500,
            details={"status": status},
        )

    def _timeout_error(self, err: Exception, *, task_id: str | None) -> MalkuthError:
        """Timeout 을 구조화 에러로 변환한다."""
        return MalkuthError(
            category=ErrorCategory.TIMEOUT,
            code=ErrorCode.NET_002,
            message="control api request timed out",
            agent=self._agent,
            task_id=task_id,
            retryable=True,
        )

    def _transport_error(self, err: Exception, *, task_id: str | None) -> MalkuthError:
        """연결 실패를 구조화 에러로 변환한다."""
        return MalkuthError(
            category=ErrorCategory.NETWORK,
            code=ErrorCode.NET_001,
            message="control api is unreachable",
            agent=self._agent,
            task_id=task_id,
            retryable=True,
        )


def control_url(host: str, port: int = DEFAULT_CONTROL_PORT) -> str:
    """Build a Control API base URL.

    Control API 기본 URL 을 만듭니다 — 포트는 runtime 이 결정합니다.
    """
    return f"http://{host}:{port}"
