"""Declaration names never leave their root (#273).

이름이 URL 에서 곧장 파일 경로가 된다. 검증 없이 이으면 ``DELETE /v1/agents/..`` 가 에이전트
루트 밖의 ``manifest.yaml`` 을 지우고, ``GET`` 은 루트 밖 파일을 파싱해 오류로 흘렸다.

HTTP 테스트는 ASGI scope 를 직접 만든다 — httpx 는 ``..`` 구간을 보내기 전에 정규화해 버려서,
실제 공격 요청이 라우트에 닿는 경로를 재현하지 못한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from malkuth.authoring import Author
from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.materials import InMemoryMaterialStore
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import InMemoryRunStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DECOY = "decoy: outside every declaration root\n"

BAD_NAMES = [
    "..",
    ".",
    "../outside",
    "a/b",
    "a\\b",
    "",
    "Alpha",
    "-alpha",
    "alpha-",
    "al..pha",
    "alpha.yaml",
    "%2e%2e",
]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """실제 저장소의 선언 사본 — 그리고 루트 밖, 에이전트 루트의 부모에 미끼 파일."""
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        _copy(REPO_ROOT / name, root / name)
    (root / "manifest.yaml").write_text(DECOY, encoding="utf-8")
    (root / ".yaml").write_text(DECOY, encoding="utf-8")
    return root


def _copy(source: Path, target: Path) -> None:
    import shutil

    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))


@pytest.fixture
def catalog(workspace: Path) -> Catalog:
    return Catalog.under(workspace)


# --- 카탈로그: 읽기 ----------------------------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
@pytest.mark.parametrize("kind", ["agent", "graph", "group"])
def test_a_lookup_with_a_bad_name_is_refused_before_touching_disk(catalog, kind, name):
    with pytest.raises(MalkuthError) as exc_info:
        getattr(catalog, kind)(name)

    assert exc_info.value.code == ErrorCode.VAL_002
    assert exc_info.value.details == {"kind": kind, "name": name}


def test_a_valid_name_still_resolves(catalog):
    assert catalog.agent("planner").name == "planner"
    assert catalog.graph("research-pipeline").metadata.name == "research-pipeline"


def test_a_symlink_that_leaves_the_root_is_refused(catalog, workspace, tmp_path):
    """규칙에 맞는 이름이라도 해석한 경로가 루트 밖이면 막는다 — 규칙이 느슨해져도 남는 방어."""
    outside = tmp_path / "elsewhere" / "escape"
    outside.mkdir(parents=True)
    # 이름까지 맞춘 온전한 매니페스트 — 포함 확인이 없으면 그대로 읽힌다
    document = yaml.safe_load((workspace / "agents" / "planner" / "manifest.yaml").read_text())
    document["metadata"]["name"] = "escape"
    (outside / "manifest.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    (workspace / "agents" / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(MalkuthError) as exc_info:
        catalog.agent("escape")

    assert exc_info.value.code == ErrorCode.VAL_002


# --- 저작: 쓰기와 삭제 -------------------------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_deleting_with_a_bad_name_removes_nothing(workspace, catalog, name):
    author = Author(catalog=catalog)

    for delete in (author.delete_agent, author.delete_graph):
        with pytest.raises(MalkuthError) as exc_info:
            delete(name)
        assert exc_info.value.code == ErrorCode.VAL_002

    assert (workspace / "manifest.yaml").read_text() == DECOY
    assert (workspace / ".yaml").read_text() == DECOY


# --- HTTP: 공격 요청 그대로 ---------------------------------------------------------


async def _raw(app, method: str, raw_path: str, body: dict | None = None) -> tuple[int, str]:
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http",
        "method": method,
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "http_version": "1.1",
        "scheme": "http",
        "server": ("cp", 80),
        "client": ("test", 1),
        "root_path": "",
    }
    status: dict = {"body": b""}

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            status["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return status["code"], status["body"].decode()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("DELETE", "/v1/agents/..", None),
        ("DELETE", "/v1/graphs/..", None),
        ("GET", "/v1/agents/..", None),
        ("GET", "/v1/graphs/..", None),
        ("GET", "/v1/groups/..", None),
        ("GET", "/v1/agents/../materials", None),
        ("PUT", "/v1/agents/../materials", {"files": {"src/a.py": "x = 1\n"}}),
        ("DELETE", "/v1/agents/../materials", None),
    ],
)
async def test_a_dot_segment_request_is_a_400_and_leaves_the_decoy(
    workspace, catalog, method, path, body
):
    app = create_app(
        InMemoryRunStore(),
        catalog=catalog,
        author=Author(catalog=catalog, materials=InMemoryMaterialStore()),
    )

    code, text = await _raw(app, method, path, body)

    assert code == 400, text
    assert json.loads(text)["error"]["code"] == ErrorCode.VAL_002
    assert str(workspace) not in text, "응답이 서버의 절대 경로를 흘렸다"
    assert (workspace / "manifest.yaml").read_text() == DECOY
