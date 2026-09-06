"""Deployment routes — the control plane fronting the manager (#243)."""

from __future__ import annotations

import httpx
import pytest

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import InMemoryRunStore
from malkuth.runtime.deployments import DeployedAgent, DeploymentRecord, DeploymentStatus


class FakeManager:
    """라우트가 manager 계약을 어떻게 부르는지만 본다 — 컨테이너는 단위 테스트 소관."""

    def __init__(self) -> None:
        self.records: dict[str, DeploymentRecord] = {}
        self.reattached = 0
        self.author = None

    async def deploy(self, graph: str) -> DeploymentRecord:
        if graph == "broken":
            raise MalkuthError(
                category=ErrorCategory.VALIDATION, code=ErrorCode.VAL_001, message="invalid"
            )
        if graph == "nope":
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND, code=ErrorCode.NF_001, message="unknown graph"
            )
        record = DeploymentRecord(
            deployment_id=f"dep-{len(self.records) + 1}",
            graph=graph,
            version="1.0.0",
            status=DeploymentStatus.READY,
            agents=(
                DeployedAgent(
                    name="planner",
                    replica=0,
                    container_id="c" * 64,
                    image="img",
                    control_port=1,
                    token="SECRET",
                ),
            ),
        )
        self.records[record.deployment_id] = record
        return record

    def deployments(self):
        return list(self.records.values())

    def get(self, deployment_id: str) -> DeploymentRecord:
        try:
            return self.records[deployment_id]
        except KeyError:
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown deployment",
            ) from None

    async def teardown(self, deployment_id: str) -> DeploymentRecord:
        record = self.get(deployment_id)
        stopped = DeploymentRecord(**{**record.__dict__, "status": DeploymentStatus.STOPPED})
        self.records[deployment_id] = stopped
        return stopped

    def in_use(self, kind: str, name: str) -> bool:
        return False

    async def reattach(self):
        self.reattached += 1
        return []


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
async def api(manager):
    app = create_app(InMemoryRunStore(), deployments=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


async def test_post_deploys_and_returns_201(api):
    response = await api.post("/v1/deployments", json={"graph": "research-pipeline"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready" and body["graph"] == "research-pipeline"
    assert body["agents"][0]["name"] == "planner"


async def test_the_token_is_never_in_a_response(api):
    body = (await api.post("/v1/deployments", json={"graph": "g"})).json()

    assert "SECRET" not in str(body)
    assert len(body["agents"][0]["container_id"]) == 12


async def test_a_validation_failure_is_400(api):
    response = await api.post("/v1/deployments", json={"graph": "broken"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_001


async def test_an_unknown_graph_is_404(api):
    assert (await api.post("/v1/deployments", json={"graph": "nope"})).status_code == 404


async def test_a_bad_body_is_400_val_002(api):
    response = await api.post("/v1/deployments", json={"nope": 1})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


async def test_list_and_get(api):
    created = (await api.post("/v1/deployments", json={"graph": "g"})).json()

    listed = (await api.get("/v1/deployments")).json()["items"]
    fetched = (await api.get(f"/v1/deployments/{created['deployment_id']}")).json()

    assert [d["deployment_id"] for d in listed] == [created["deployment_id"]]
    assert fetched == created


async def test_delete_tears_down_and_reports_stopped(api):
    created = (await api.post("/v1/deployments", json={"graph": "g"})).json()

    response = await api.delete(f"/v1/deployments/{created['deployment_id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"


async def test_unknown_deployment_is_404(api):
    assert (await api.get("/v1/deployments/dep-nope")).status_code == 404
    assert (await api.delete("/v1/deployments/dep-nope")).status_code == 404


async def test_startup_reattaches(manager):
    """재시작 뒤 살아 있는 컨테이너를 기록과 대조한다 — 서버 루프 안에서."""
    app = create_app(InMemoryRunStore(), deployments=manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp"):
        pass

    # ASGITransport 는 lifespan 을 돌리지 않는다 — lifespan 을 직접 태운다
    async with app.router.lifespan_context(app):
        pass

    assert manager.reattached == 1


async def test_without_a_manager_the_routes_do_not_exist():
    app = create_app(InMemoryRunStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as api:
        assert (await api.get("/v1/deployments")).status_code in (404, 405)
