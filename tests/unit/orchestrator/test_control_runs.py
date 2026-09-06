"""Run submission routes (#244)."""

from __future__ import annotations

import httpx
import pytest

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.run import RunStatus
from malkuth.orchestrator.runstore import InMemoryRunStore, RunRecord
from malkuth.orchestrator.submit import RunResult
from malkuth.orchestrator.topology import GraphMode


class FakeRuns:
    def __init__(self, store: InMemoryRunStore) -> None:
        self.store = store
        self.submitted: list[tuple[str, dict, str | None]] = []
        self.results: dict[str, RunResult] = {}
        self.closed = False

    async def submit(self, deployment_id, initial_state, *, mode=None, run_id=None) -> RunRecord:
        if deployment_id == "dep-nope":
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown deployment",
            )
        self.submitted.append((deployment_id, dict(initial_state), mode))
        record = RunRecord(run_id=run_id or "run-new", graph="g", mode="mission", status="running")
        self.store.upsert(record)
        return record

    async def resume(self, run_id) -> RunRecord:
        record = RunRecord(run_id=run_id, graph="g", mode="service", status="running")
        self.store.upsert(record)
        return record

    def result_of(self, run_id):
        return self.results.get(run_id)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def store() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
def runs(store) -> FakeRuns:
    return FakeRuns(store)


@pytest.fixture
async def api(store, runs):
    app = create_app(store, runs=runs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


async def test_post_submits_and_returns_202_immediately(api, runs):
    response = await api.post("/v1/runs", json={"deployment_id": "dep-1", "input": {"query": "q"}})

    assert response.status_code == 202
    assert response.json()["run_id"] == "run-new" and response.json()["status"] == "running"
    assert runs.submitted == [("dep-1", {"query": "q"}, None)]


async def test_post_with_an_unknown_deployment_is_404(api):
    response = await api.post("/v1/runs", json={"deployment_id": "dep-nope"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NF_001


async def test_post_with_a_bad_body_is_400_val_002(api):
    response = await api.post("/v1/runs", json={"input": {}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


async def test_get_shows_the_final_state_once_the_run_finished(api, store, runs):
    store.upsert(RunRecord(run_id="run-1", graph="g", mode="mission", status="completed"))
    runs.results["run-1"] = RunResult(
        run_id="run-1",
        graph="g",
        mode=GraphMode.MISSION,
        status=RunStatus.COMPLETED,
        state={"report": "done"},
    )

    body = (await api.get("/v1/runs/run-1")).json()

    assert body["status"] == "completed" and body["state"] == {"report": "done"}
    assert body["error"] is None


async def test_get_of_a_running_run_has_no_state_yet(api, store):
    store.upsert(RunRecord(run_id="run-1", graph="g", mode="mission", status="running"))

    body = (await api.get("/v1/runs/run-1")).json()

    assert body["status"] == "running" and "state" not in body


async def test_resume_is_driven_by_the_run_service(api, store):
    """501 이던 자리 — 이 프로세스가 구동 프로세스가 됐다."""
    store.upsert(RunRecord(run_id="halted", graph="g", mode="service", status="halted"))

    response = await api.post("/v1/runs/halted/resume")

    assert response.status_code == 200
    assert response.json()["run_id"] == "halted" and response.json()["status"] == "resumed"


async def test_shutdown_closes_the_run_service(store, runs):
    app = create_app(store, runs=runs)
    async with app.router.lifespan_context(app):
        pass

    assert runs.closed
