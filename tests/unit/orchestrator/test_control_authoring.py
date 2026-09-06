"""Authoring over HTTP — validated before anything touches disk (#242)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from malkuth.authoring import Author
from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import InMemoryRunStore

REPO_ROOT = Path(__file__).resolve().parents[3]


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def graph_doc(name: str, version: str = "1.0.0", agent: str = "alpha") -> dict:
    return {
        "apiVersion": "malkuth/v1",
        "kind": "Graph",
        "metadata": {"name": name, "version": version},
        "spec": {
            "mode": "mission",
            "goal": "test",
            "state": {"schema": "malkuth.graphs.schemas:ResearchState"},
            "nodes": [{"id": "step", "agent": f"agents/{agent}@0.1.0"}],
            "edges": [{"from": "START", "to": "step"}, {"from": "step", "to": "END"}],
        },
    }


def agent_doc(name: str, version: str = "0.1.0") -> dict:
    return {
        "apiVersion": "malkuth/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": version},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/solo@0.1.0"},
        },
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "malkuth/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {
                "engine": "jinja2",
                "templates": {"default": {"file": "t.j2"}, "step": {"file": "s.j2"}},
            },
        },
    )
    write(tmp_path / "agents" / "alpha" / "manifest.yaml", agent_doc("alpha"))
    write(
        tmp_path / "groups" / "global.yaml",
        yaml.safe_load((REPO_ROOT / "groups" / "global.yaml").read_text(encoding="utf-8")),
    )
    (tmp_path / "graphs").mkdir()
    return tmp_path


@pytest.fixture
async def api(workspace):
    catalog = Catalog.under(workspace)
    app = create_app(InMemoryRunStore(), catalog=catalog, author=Author(catalog=catalog))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


async def test_a_valid_graph_is_saved_and_then_listed(api, workspace):
    response = await api.put("/v1/graphs/pipeline", json=graph_doc("pipeline"))

    assert response.status_code == 200
    assert (workspace / "graphs" / "pipeline.yaml").exists()
    listed = (await api.get("/v1/graphs")).json()
    assert [i["name"] for i in listed["items"]] == ["pipeline"]


async def test_an_invalid_graph_is_400_with_findings_and_no_file(api, workspace):
    response = await api.put("/v1/graphs/bad", json=graph_doc("bad", agent="nobody"))

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == ErrorCode.VAL_001
    assert any("nobody" in str(f) for f in body["details"]["findings"])
    assert not (workspace / "graphs" / "bad.yaml").exists()


async def test_a_malformed_body_names_the_field(api):
    response = await api.put("/v1/graphs/x", json={"apiVersion": "malkuth/v1", "kind": "Graph"})

    assert response.status_code == 400
    errors = response.json()["error"]["details"]["errors"]
    assert any(e["field"] == "metadata" for e in errors)


async def test_validate_reports_without_saving(api, workspace):
    response = await api.post("/v1/validate", json={"graphs": [graph_doc("draft", agent="nobody")]})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["findings"]
    assert not (workspace / "graphs" / "draft.yaml").exists()


async def test_validate_accepts_a_draft_agent_and_graph_together(api):
    response = await api.post(
        "/v1/validate",
        json={"agents": [agent_doc("beta")], "graphs": [graph_doc("g", agent="beta")]},
    )

    assert response.json()["ok"] is True


async def test_a_change_without_a_bump_is_rejected(api):
    await api.put("/v1/graphs/pipeline", json=graph_doc("pipeline"))
    changed = graph_doc("pipeline")
    changed["spec"]["goal"] = "other"

    response = await api.put("/v1/graphs/pipeline", json=changed)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.MOD_002


async def test_deleting_a_referenced_agent_is_refused(api, workspace):
    await api.put("/v1/graphs/pipeline", json=graph_doc("pipeline"))

    response = await api.delete("/v1/agents/alpha")

    assert response.status_code == 400
    assert response.json()["error"]["details"]["referenced_by"] == ["pipeline"]
    assert (workspace / "agents" / "alpha" / "manifest.yaml").exists()


async def test_deleting_a_graph_is_204_and_gone(api, workspace):
    await api.put("/v1/graphs/pipeline", json=graph_doc("pipeline"))

    response = await api.delete("/v1/graphs/pipeline")

    assert response.status_code == 204
    assert not (workspace / "graphs" / "pipeline.yaml").exists()


async def test_deleting_a_missing_graph_is_404(api):
    assert (await api.delete("/v1/graphs/nope")).status_code == 404


async def test_a_saved_agent_is_readable_back(api):
    response = await api.put("/v1/agents/beta", json=agent_doc("beta"))

    assert response.status_code == 200
    assert (await api.get("/v1/agents/beta")).json()["metadata"]["name"] == "beta"


async def test_modules_have_no_write_route(api):
    """04 Registry 2 — 게시된 모듈은 불변. PUT 이 있으면 그것이 위반이다."""
    response = await api.put("/v1/modules/promptsets/solo/0.2.0", json={})

    assert response.status_code == 405


async def test_without_an_author_the_write_routes_do_not_exist(workspace):
    catalog = Catalog.under(workspace)
    app = create_app(InMemoryRunStore(), catalog=catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as api:
        response = await api.put("/v1/graphs/pipeline", json=graph_doc("pipeline"))

    assert response.status_code == 405
    assert not (workspace / "graphs" / "pipeline.yaml").exists()
