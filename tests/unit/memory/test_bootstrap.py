"""Memory Service assembly.

`create_app` 은 **이미 조립된** 컴포넌트 넷을 요구한다 — 그것을 설정과 선언
으로 만드는 곳이 없어 서비스를 프로세스로 세울 수 없었다 (#181).

09 Access Enforcement 1 의 배치가 여기서 결정된다: 저장소 자격증명은 이
프로세스만 갖고, 에이전트는 불투명 토큰으로만 닿는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from malkuth.config import load_config
from malkuth.memory.bootstrap import build_deployment

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def deployment():
    """저장소 선언 그대로 조립한 서비스."""
    config = load_config("dev", config_dir=REPO_ROOT / "configs", environ={})
    return build_deployment(config, root=REPO_ROOT)


def test_every_declared_agent_gets_a_token(deployment):
    """토큰을 못 받은 에이전트는 자기 space 에 닿을 방법이 없다."""
    declared = {path.parent.name for path in (REPO_ROOT / "agents").glob("*/manifest.yaml")}

    assert set(deployment.tokens) == declared


def test_tokens_are_opaque_and_distinct(deployment):
    """페이로드를 넘기면 space 목록과 mode 를 위조할 수 있다 (09)."""
    issued = list(deployment.tokens.values())

    assert len(set(issued)) == len(issued)
    for token in issued:
        assert "researcher" not in token
        assert "longterm" not in token


def test_the_app_refuses_an_unauthenticated_request(deployment):
    """무토큰 접근이 통하면 space 경계가 무의미해진다."""
    with TestClient(deployment.app) as client:
        assert client.get("/v1/spaces").status_code == 401


def test_an_agent_sees_exactly_its_declared_spaces(deployment):
    """선언되지 않은 space 가 보이면 그것은 이미 경계 밖이다."""
    token = deployment.tokens["researcher"]

    with TestClient(deployment.app) as client:
        spaces = client.get("/v1/spaces", headers={"Authorization": f"Bearer {token}"}).json()

    scopes = {entry["alias"]: entry["scope"] for entry in spaces}
    # manifest 의 local, 소속 그룹의 group, groups/global.yaml 의 global
    assert scopes["longterm"] == "local"
    assert scopes["knowledge"] == "group"
    assert scopes["org"] == "global"


def test_a_non_member_does_not_see_the_group_space(deployment):
    """그룹 소속이 곧 접근 경계다 (09 Scope Rules 3)."""
    token = deployment.tokens["echo"]

    with TestClient(deployment.app) as client:
        spaces = client.get("/v1/spaces", headers={"Authorization": f"Bearer {token}"}).json()

    assert "knowledge" not in {entry["alias"] for entry in spaces}


def test_a_global_space_rejects_a_write_from_a_non_writer(deployment):
    """`writers` 미지정이면 전 에이전트 read-only — 전사 지식은 아무나 쓰면 오염된다."""
    token = deployment.tokens["researcher"]
    payload = {
        "space": "org",
        "entry": {
            "space": "org",
            "kind": "fact",
            "content": "오염 시도",
            "source": {"agent": "researcher"},
        },
    }

    with TestClient(deployment.app) as client:
        response = client.post(
            "/v1/append", json=payload, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 401


def test_a_forged_token_is_refused(deployment):
    """토큰을 서비스가 기억하므로 지어내도 통하지 않는다."""
    with TestClient(deployment.app) as client:
        response = client.get("/v1/spaces", headers={"Authorization": "Bearer forged"})

    assert response.status_code == 401
