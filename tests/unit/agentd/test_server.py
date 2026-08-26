"""Unit tests for the Agent Control API server.

컨테이너 없이 FastAPI TestClient 로 검증한다 — 이 계층의 계약은 HTTP 표면
(엔드포인트·인증·동시성·에러 변환)이지 실행 로직이 아니다.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from malkuth.agentd.server import AgentRuntime, create_app
from malkuth.core.agent import ComponentHealth, HealthState, HealthStatus, TaskResult, TaskStatus
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.events import DoneEvent, TokenEvent
from tests.fixtures.builders import make_task

TOKEN = "agent-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class FakeExecutor:
    """태스크 실행을 스크립트하는 executor 대역."""

    def __init__(self, result=None, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self._result = result
        self._delay = delay
        self._error = error
        self.calls: list[str] = []
        self.running = asyncio.Event()

    async def execute(self, task):
        self.calls.append(task.task_id)
        self.running.set()
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result or TaskResult.completed(task, output={"content": "done"})

    async def stream(self, task):
        self.calls.append(task.task_id)
        yield TokenEvent(task_id=task.task_id, text="hi")
        yield DoneEvent(task_id=task.task_id, output={"content": "done"})


def make_client(
    executor=None, *, raise_server_exceptions: bool = True, **runtime_kwargs
) -> tuple[TestClient, AgentRuntime]:
    """토큰이 걸린 앱과 런타임.

    ``raise_server_exceptions=False`` 는 실제 서버와 같이 동작한다 — 기본값은
    디버깅용으로 예외를 다시 던져 exception handler 를 건너뛴다.
    """
    runtime = AgentRuntime(
        agent="researcher", executor=executor or FakeExecutor(), **runtime_kwargs
    )
    client = TestClient(
        create_app(runtime, token=TOKEN), raise_server_exceptions=raise_server_exceptions
    )
    return client, runtime


def task_payload(**overrides) -> dict:
    """직렬화된 TaskRequest 본문."""
    return make_task(**overrides).model_dump(mode="json")


# --- 인증 ------------------------------------------------------------------


def test_health_is_unauthenticated():
    """Docker healthcheck 가 직접 호출하므로 토큰을 요구하지 않는다."""
    client, _ = make_client()

    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/v1/invoke", True),
        ("post", "/v1/stream", True),
        ("get", "/v1/card", False),
        ("post", "/v1/cancel/t1", False),
        ("post", "/v1/reload", False),
        ("post", "/v1/drain", False),
    ],
)
def test_authenticated_endpoints_reject_missing_token(method, path, body):
    client, _ = make_client()

    kwargs = {"json": task_payload()} if body else {}
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_wrong_token_is_rejected():
    client, _ = make_client()

    response = client.get("/v1/card", headers={"authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_valid_token_is_accepted():
    client, _ = make_client()

    assert client.get("/v1/card", headers=AUTH).status_code == 200


def test_app_without_token_skips_authentication():
    """토큰 미설정 환경(로컬 개발)에서는 인증을 요구하지 않는다."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())
    client = TestClient(create_app(runtime))

    assert client.get("/v1/card").status_code == 200


# --- invoke ----------------------------------------------------------------


def test_invoke_returns_a_task_result():
    client, _ = make_client()

    response = client.post("/v1/invoke", json=task_payload(task_id="t-1"), headers=AUTH)

    body = response.json()
    assert response.status_code == 200
    assert body["task_id"] == "t-1"
    assert body["status"] == TaskStatus.COMPLETED


def test_invoke_is_synchronous_not_accepted():
    """202+polling 이 아니라 동기 응답이다 (02 API Rules 2)."""
    client, _ = make_client()

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 200


def test_invoke_response_is_a_serialized_model():
    """ad-hoc dict 가 아니라 TaskResult 직렬화여야 한다."""
    client, _ = make_client()

    body = client.post("/v1/invoke", json=task_payload(), headers=AUTH).json()

    assert set(body) >= {"task_id", "status", "output", "usage", "completed_at"}


def test_malformed_task_is_rejected():
    client, _ = make_client()

    response = client.post("/v1/invoke", json={"task_id": "only"}, headers=AUTH)

    assert response.status_code == 422


# --- 에러 변환 -------------------------------------------------------------


def test_uncaught_exception_becomes_a_structured_error():
    """예상 못한 예외가 500 빈 응답이 되면 원인을 알 수 없다.

    TestClient 기본값은 디버깅을 위해 예외를 다시 던지므로, 실제 서버와 같은
    동작(handler 가 응답으로 변환)을 보려면 raise_server_exceptions=False 가 필요하다.
    """
    client, _ = make_client(FakeExecutor(error=RuntimeError("boom")), raise_server_exceptions=False)

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == ErrorCode.INTERNAL_001
    assert body["category"] == ErrorCategory.INTERNAL
    assert body["agent"] == "researcher"


def test_structured_error_keeps_its_payload():
    error = MalkuthError(
        category=ErrorCategory.MODEL,
        code=ErrorCode.LLM_005,
        message="max turns exceeded",
    )
    client, _ = make_client(FakeExecutor(error=error))

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 400
    assert response.json()["code"] == "LLM_005"


def test_retryable_error_reports_service_unavailable():
    """재시도 가능 실패는 503 이어야 호출자가 재시도를 판단할 수 있다."""
    error = MalkuthError(
        category=ErrorCategory.TIMEOUT,
        code=ErrorCode.TO_001,
        message="task timeout",
        retryable=True,
    )
    client, _ = make_client(FakeExecutor(error=error))

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 503


# --- stream ----------------------------------------------------------------


def test_stream_emits_sse_events():
    client, _ = make_client()

    with client.stream("POST", "/v1/stream", json=task_payload(), headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert body.count("data: ") == 2
    assert '"type":"token"' in body.replace(" ", "")


# --- card / cancel / reload / drain -----------------------------------------


def test_card_returns_the_agent_card():
    client, _ = make_client(card={"name": "researcher", "capabilities": {"streaming": True}})

    body = client.get("/v1/card", headers=AUTH).json()

    assert body["name"] == "researcher"


def test_cancel_unknown_task_is_not_found():
    client, _ = make_client()

    response = client.post("/v1/cancel/missing", headers=AUTH)

    assert response.status_code == 400
    assert response.json()["code"] == "NF_001"


def test_reload_invokes_the_hook():
    reloaded = asyncio.Event()

    async def reload() -> None:
        reloaded.set()

    client, _ = make_client(reload=reload)

    response = client.post("/v1/reload", headers=AUTH)

    assert response.json()["status"] == "reloaded"
    assert reloaded.is_set()


def test_reload_without_a_hook_is_accepted():
    client, _ = make_client()

    assert client.post("/v1/reload", headers=AUTH).json()["status"] == "reloaded"


def test_drain_marks_the_runtime_draining():
    client, runtime = make_client()

    response = client.post("/v1/drain", headers=AUTH)

    assert response.json()["status"] == "drained"
    assert runtime.draining is True


def test_draining_agent_rejects_new_tasks():
    """drain 중 새 태스크를 받으면 정지가 끝나지 않는다."""
    client, _ = make_client()
    client.post("/v1/drain", headers=AUTH)

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "RT_005"


def test_draining_agent_rejects_streaming_too():
    client, _ = make_client()
    client.post("/v1/drain", headers=AUTH)

    response = client.post("/v1/stream", json=task_payload(), headers=AUTH)

    assert response.status_code == 503


def test_draining_reports_degraded_health():
    client, _ = make_client()
    client.post("/v1/drain", headers=AUTH)

    assert client.get("/v1/health").json()["status"] == HealthState.DEGRADED


def test_health_reflects_component_state():
    def health() -> HealthStatus:
        return HealthStatus.aggregate({"mcp:fs": ComponentHealth(state=HealthState.DEGRADED)})

    client, _ = make_client(health=health)

    body = client.get("/v1/health").json()

    assert body["status"] == HealthState.DEGRADED
    assert "mcp:fs" in body["components"]


# --- 동시성 상한 ------------------------------------------------------------


async def test_concurrency_is_capped_by_the_semaphore():
    """direct 요청과 그래프 태스크가 같은 큐를 공유한다 (02 Direct Request Rules 5)."""
    runtime = AgentRuntime(
        agent="researcher", executor=FakeExecutor(delay=0.05), max_concurrent_tasks=2
    )

    assert runtime.semaphore._value == 2

    async with runtime.semaphore, runtime.semaphore:
        assert runtime.semaphore.locked()


async def test_cancel_stops_an_inflight_task():
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor(delay=5))
    task = make_task(task_id="t-cancel")
    running = asyncio.create_task(runtime.executor.execute(task))
    runtime.track(task.task_id, running)

    assert runtime.cancel("t-cancel") is True

    with pytest.raises(asyncio.CancelledError):
        await running


async def test_cancel_reports_false_for_unknown_task():
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())

    assert runtime.cancel("nope") is False


async def test_drain_waits_for_inflight_tasks():
    """진행 중 태스크를 마친 뒤 정지한다 — 즉시 중단이 아니다."""
    executor = FakeExecutor(delay=0.05)
    runtime = AgentRuntime(agent="researcher", executor=executor)
    task = make_task(task_id="t-drain")
    running = asyncio.create_task(runtime.executor.execute(task))
    runtime.track(task.task_id, running)
    await executor.running.wait()

    await runtime.drain()

    assert running.done()
    assert runtime.draining is True


async def test_completed_tasks_are_untracked():
    """추적 목록이 무한히 자라면 메모리가 샌다."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())
    task = make_task(task_id="t-done")
    running = asyncio.create_task(runtime.executor.execute(task))
    runtime.track(task.task_id, running)

    await running
    await asyncio.sleep(0)  # done callback 이 돌 기회를 준다

    assert runtime.cancel("t-done") is False
