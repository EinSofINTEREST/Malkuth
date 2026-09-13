"""Image build routes (#265)."""

from __future__ import annotations

import httpx
import pytest

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import InMemoryRunStore
from malkuth.runtime.images import BuildRecord, BuildStatus


class FakeBuilder:
    """라우트가 빌더 계약을 어떻게 부르는지만 본다 — 조립은 단위 테스트 소관."""

    class _Catalog:
        def agent(self, name: str):
            if name == "nobody":
                raise MalkuthError(
                    category=ErrorCategory.NOT_FOUND, code=ErrorCode.NF_001, message="unknown agent"
                )
            return type("M", (), {"metadata": type("Meta", (), {"version": "0.1.0"})()})()

    def __init__(self) -> None:
        self.catalog = self._Catalog()
        self.records: dict[tuple[str, str], BuildRecord] = {}
        self.custom = {"custom"}

    def needs_build(self, agent: str, version: str) -> bool:
        return agent in self.custom

    def record_of(self, agent: str, version: str) -> BuildRecord | None:
        return self.records.get((agent, version))

    async def build(self, agent: str) -> BuildRecord:
        self.catalog.agent(agent)
        if agent not in self.custom:
            raise MalkuthError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="agent has no build materials",
            )
        record = BuildRecord(
            agent=agent,
            version="0.1.0",
            status=BuildStatus.BUILT,
            image=f"malkuth/agent-{agent}:0.1.0",
            log="Step 1/3",
        )
        self.records[agent, "0.1.0"] = record
        return record


@pytest.fixture
def builder() -> FakeBuilder:
    return FakeBuilder()


@pytest.fixture
async def api(builder):
    app = create_app(InMemoryRunStore(), builder=builder)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as client:
        yield client


async def test_a_build_reports_the_image_it_made(api):
    response = await api.post("/v1/agents/custom/image")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == BuildStatus.BUILT
    assert body["image"] == "malkuth/agent-custom:0.1.0"


async def test_an_agent_that_was_never_built_says_so(api):
    body = (await api.get("/v1/agents/custom/image")).json()

    assert body["status"] is None
    assert body["needs_build"] is True
    assert body["image"] == "malkuth/agent-custom:0.1.0"


async def test_the_status_follows_the_build(api):
    await api.post("/v1/agents/custom/image")

    body = (await api.get("/v1/agents/custom/image")).json()

    assert body["status"] == BuildStatus.BUILT
    assert body["log"] == "Step 1/3"


async def test_a_declarative_agent_does_not_need_a_build(api):
    """base 이미지로 도는 에이전트를 굽게 만들면 모듈 시스템의 이점이 사라진다."""
    body = (await api.get("/v1/agents/plain/image")).json()

    assert body["needs_build"] is False


async def test_building_an_agent_without_materials_is_400(api):
    response = await api.post("/v1/agents/plain/image")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


async def test_an_unknown_agent_is_404(api):
    assert (await api.post("/v1/agents/nobody/image")).status_code == 404
    assert (await api.get("/v1/agents/nobody/image")).status_code == 404


async def test_without_a_builder_the_routes_do_not_exist():
    app = create_app(InMemoryRunStore())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as api:
        assert (await api.get("/v1/agents/custom/image")).status_code in (404, 405)
