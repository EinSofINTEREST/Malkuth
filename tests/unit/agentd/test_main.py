"""Unit tests for the agentd entrypoint.

컨테이너 없이 검증한다 — 이 계층의 계약은 manifest 로드와 앱 구성이지
프로세스 기동이 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from malkuth.agentd.__main__ import CONTROL_PORT, build_app, load_manifest
from malkuth.agentd.echo import EchoExecutor
from malkuth.core.agent import TaskStatus
from malkuth.core.manifest import AgentManifest
from tests.fixtures.builders import make_task

ECHO_MANIFEST = Path(__file__).resolve().parents[3] / "agents" / "echo" / "manifest.yaml"


def write(tmp_path: Path, body: str) -> Path:
    """manifest 파일을 만든다."""
    path = tmp_path / "manifest.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- manifest 로드 -----------------------------------------------------------


def test_control_port_is_the_declared_one():
    """컨테이너 내부 control port 는 8080 고정 (02 Network)."""
    assert CONTROL_PORT == 8080


def test_shipped_echo_manifest_is_valid():
    """이미지에 실려 나가는 manifest 가 스키마를 통과해야 한다."""
    manifest = load_manifest(ECHO_MANIFEST)

    assert manifest.name == "echo"
    assert manifest.spec.runtime.image == "malkuth/agent-echo:0.1.0"


def test_missing_manifest_is_a_config_error(tmp_path: Path):
    """읽을 수 없는 manifest 로는 기동하지 않는다."""
    with pytest.raises(Exception) as exc_info:
        load_manifest(tmp_path / "absent.yaml")

    assert exc_info.value.code == "CFG_001"


def test_invalid_manifest_is_rejected(tmp_path: Path):
    """미검증 manifest 로 컨테이너를 기동하지 않는다 (02 Manifest Rules 1)."""
    path = write(tmp_path, "apiVersion: malkuth/v1\nkind: Agent\nmetadata: {}\n")

    with pytest.raises(Exception) as exc_info:
        load_manifest(path)

    assert exc_info.value.code == "CFG_001"


def test_unparseable_yaml_is_rejected(tmp_path: Path):
    path = write(tmp_path, "apiVersion: [unclosed\n")

    with pytest.raises(Exception) as exc_info:
        load_manifest(path)

    assert exc_info.value.code == "CFG_001"


# --- 앱 구성 -----------------------------------------------------------------


def make_client(manifest: AgentManifest, *, token: str | None = None) -> TestClient:
    return TestClient(build_app(manifest, EchoExecutor(), token=token))


def test_card_is_derived_from_the_manifest():
    """AgentCard 는 manifest 에서 생성한다 — 수동 작성 금지 (03 AgentCard)."""
    manifest = load_manifest(ECHO_MANIFEST)

    body = make_client(manifest).get("/v1/card").json()

    assert body["name"] == "echo"
    assert body["version"] == "0.1.0"


def test_app_serves_health_unauthenticated():
    """healthcheck 가 토큰 없이 호출한다."""
    manifest = load_manifest(ECHO_MANIFEST)

    response = make_client(manifest, token="secret").get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_app_requires_the_token_on_other_endpoints():
    manifest = load_manifest(ECHO_MANIFEST)

    assert make_client(manifest, token="secret").get("/v1/card").status_code == 401


def test_concurrency_limit_comes_from_the_manifest(tmp_path: Path):
    """manifest 가 선언한 상한을 그대로 쓴다 — 하드코딩 금지."""
    path = write(
        tmp_path,
        "apiVersion: malkuth/v1\nkind: Agent\n"
        "metadata: {name: sample, version: 0.1.0}\n"
        "spec:\n"
        "  model: {provider: anthropic, name: claude-sonnet-5}\n"
        "  promptset: {ref: promptsets/sample@0.1.0}\n"
        "  runtime: {max_concurrent_tasks: 2}\n",
    )
    manifest = load_manifest(path)

    app = build_app(manifest, EchoExecutor())

    client = TestClient(app)
    assert client.get("/v1/health").status_code == 200


# --- echo 실행기 -------------------------------------------------------------


async def test_echo_returns_the_input():
    task = make_task(input={"msg": "hi"})

    result = await EchoExecutor().execute(task)

    assert result.status == TaskStatus.COMPLETED
    assert result.output == {"msg": "hi"}


async def test_echo_streams_token_then_done():
    task = make_task(input={"msg": "hi"})

    events = [event async for event in EchoExecutor().stream(task)]

    assert [e.type for e in events] == ["token", "done"]
    assert events[-1].output == {"msg": "hi"}
