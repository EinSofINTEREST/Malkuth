"""Access registry routes — three audiences, three credentials (#277).

토큰 하나로 묶으면 강제 지점이 운영자 권한을, 권한 에이전트가 판정 조회를 덤으로 갖는다. 각 라우트가
**자기 호출자의 자격만** 받는지 본다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from malkuth.access.registry import AccessRegistry
from malkuth.access.store import InMemoryAccessStore
from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.access import KNOWLEDGE, STEWARD, access_workspace

CONTROL = "control-token"
ENFORCER = "enforcer-token"


@pytest.fixture
def registry(tmp_path: Path) -> AccessRegistry:
    catalog = Catalog.under(access_workspace(tmp_path))
    return AccessRegistry(
        store=InMemoryAccessStore(), catalog=catalog, stewards=frozenset({STEWARD})
    )


@pytest.fixture
async def api(registry):
    app = create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=CONTROL,
        access=registry,
        enforcer_token=ENFORCER,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


REVOCATION = {"agent": "worker", "kind": "egress", "target": "api.x", "reason": "incident"}
GRANT = {
    "agent": "worker",
    "kind": "memory",
    "target": KNOWLEDGE,
    "mode": "ro",
    "ttl_s": 60,
    "reason": "needs facts",
    "requested_by": "worker",
}


# --- 운영자 -------------------------------------------------------------------


async def test_the_operator_revokes_and_sees_the_record(api):
    created = await api.post("/v1/access/revocations", json=REVOCATION, headers=bearer(CONTROL))
    view = await api.get("/v1/access/agents/worker", headers=bearer(CONTROL))

    assert created.status_code == 201
    assert created.json()["effect"] == "deny" and created.json()["decided_by"] == "operator"
    assert [r["rule_id"] for r in view.json()["rules"]] == [created.json()["rule_id"]]


@pytest.mark.parametrize("token", [None, ENFORCER, "wrong"])
async def test_operator_routes_refuse_other_credentials(api, token):
    headers = bearer(token) if token else {}

    assert (
        await api.post("/v1/access/revocations", json=REVOCATION, headers=headers)
    ).status_code == 401
    assert (await api.get("/v1/access/agents/worker", headers=headers)).status_code == 401


async def test_lifting_ends_a_rule_without_deleting_it(api):
    created = (
        await api.post("/v1/access/revocations", json=REVOCATION, headers=bearer(CONTROL))
    ).json()

    lifted = await api.delete(f"/v1/access/rules/{created['rule_id']}", headers=bearer(CONTROL))
    view = (await api.get("/v1/access/agents/worker", headers=bearer(CONTROL))).json()

    assert lifted.json()["lifted_at"] is not None
    assert view["rules"][0]["lifted_at"] is not None


async def test_lifting_an_unknown_rule_is_404(api):
    assert (
        await api.delete("/v1/access/rules/rule-nope", headers=bearer(CONTROL))
    ).status_code == 404


@pytest.mark.parametrize(
    "change",
    [{"kind": "files"}, {"target": "api x"}, {"target": ""}, {"reason": ""}, {"extra": 1}],
)
async def test_a_malformed_revocation_is_400(api, change):
    response = await api.post(
        "/v1/access/revocations", json={**REVOCATION, **change}, headers=bearer(CONTROL)
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


# --- 권한 에이전트 --------------------------------------------------------------


async def test_the_permission_agent_grants_with_its_own_identity(api, registry):
    steward = registry.issue_identity(STEWARD, "dep-1")

    response = await api.post("/v1/access/grants", json=GRANT, headers=bearer(steward))

    assert response.status_code == 201
    assert response.json()["decided_by"] == STEWARD


@pytest.mark.parametrize("who", ["control", "enforcer", "worker", "none"])
async def test_grants_refuse_anyone_but_a_permission_agent(api, registry, who):
    """운영자 토큰도 부여하지 못한다 — 넓히기는 권한 에이전트만 (01 Access Control 3)."""
    credentials = {
        "control": CONTROL,
        "enforcer": ENFORCER,
        "worker": registry.issue_identity("worker", "dep-1"),
        "none": None,
    }
    headers = bearer(credentials[who]) if credentials[who] else {}

    response = await api.post("/v1/access/grants", json=GRANT, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] in {ErrorCode.ACC_001, ErrorCode.ACC_003}
    assert registry.rules("worker") == []


# --- 강제 지점 ------------------------------------------------------------------


async def test_an_enforcer_gets_a_decision_for_a_credential(api, registry):
    worker = registry.issue_identity("worker", "dep-1")
    steward = registry.issue_identity(STEWARD, "dep-1")
    await api.post("/v1/access/grants", json=GRANT, headers=bearer(steward))

    response = await api.post(
        "/v1/access/decisions",
        json={"credential": worker, "kind": "memory", "target": KNOWLEDGE, "mode": "ro"},
        headers=bearer(ENFORCER),
    )

    assert response.json() | {"version": 0} == {
        "agent": "worker",
        "decision": "allow",
        "decided_by": STEWARD,
        "version": 0,
    }


async def test_an_unknown_credential_is_a_cacheable_deny(api):
    response = await api.post(
        "/v1/access/decisions",
        json={"credential": "made-up", "kind": "egress", "target": "api.x"},
        headers=bearer(ENFORCER),
    )

    assert response.status_code == 200
    assert response.json()["agent"] is None and response.json()["decision"] == "deny"


@pytest.mark.parametrize("token", [None, CONTROL, "wrong"])
async def test_enforcer_routes_refuse_other_credentials(api, registry, token):
    """운영자 토큰으로도 판정을 조회하지 못한다 — 강제 지점의 토큰은 따로다."""
    headers = bearer(token) if token else {}
    body = {"credential": "x", "kind": "egress", "target": "api.x"}

    assert (await api.post("/v1/access/decisions", json=body, headers=headers)).status_code == 401
    assert (await api.get("/v1/access/changes?wait_s=0", headers=headers)).status_code == 401


async def test_the_change_feed_moves_after_a_revocation(api):
    before = (await api.get("/v1/access/changes?wait_s=0", headers=bearer(ENFORCER))).json()[
        "version"
    ]

    await api.post("/v1/access/revocations", json=REVOCATION, headers=bearer(CONTROL))
    after = await api.get(f"/v1/access/changes?after={before}&wait_s=5", headers=bearer(ENFORCER))

    assert after.json()["version"] > before


async def test_the_change_feed_caps_the_wait(api):
    response = await api.get("/v1/access/changes?wait_s=3600", headers=bearer(ENFORCER))

    assert response.status_code in (400, 422)


async def test_without_a_registry_the_routes_do_not_exist():
    app = create_app(InMemoryRunStore(), token=CONTROL)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        assert (await client.post("/v1/access/grants", json=GRANT)).status_code == 404
