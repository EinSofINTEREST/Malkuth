"""Writing declarations — nothing reaches disk without passing validation (#242)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.authoring import Author
from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.orchestrator.topology import GraphTopology

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def promptset(name: str = "solo", version: str = "0.1.0") -> dict:
    return {
        "apiVersion": "malkuth/v1",
        "kind": "Promptset",
        "metadata": {"name": name, "version": version},
        "spec": {
            "engine": "jinja2",
            "templates": {"default": {"file": "t.j2"}, "step": {"file": "s.j2"}},
        },
    }


def agent(
    name: str, version: str = "0.1.0", *, promptset_ref: str = "promptsets/solo@0.1.0"
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "apiVersion": "malkuth/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": version},
            "spec": {
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": promptset_ref},
            },
        }
    )


def graph(
    name: str, version: str = "1.0.0", *, agent_name: str = "alpha", agent_version: str = "0.1.0"
) -> GraphTopology:
    return GraphTopology.model_validate(
        {
            "apiVersion": "malkuth/v1",
            "kind": "Graph",
            "metadata": {"name": name, "version": version},
            "spec": {
                "mode": "mission",
                "goal": "test",
                "state": {"schema": "malkuth.graphs.schemas:ResearchState"},
                "nodes": [{"id": "step", "agent": f"agents/{agent_name}@{agent_version}"}],
                "edges": [{"from": "START", "to": "step"}, {"from": "step", "to": "END"}],
            },
        }
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml", promptset())
    write(
        tmp_path / "agents" / "alpha" / "manifest.yaml",
        agent("alpha").model_dump(mode="json", by_alias=True, exclude_none=True),
    )
    # 실제 저장소의 global.yaml 을 그대로 쓴다 — 손으로 만든 최소 문서는 스키마와 어긋난다
    write(
        tmp_path / "groups" / "global.yaml",
        yaml.safe_load((REPO_ROOT / "groups" / "global.yaml").read_text(encoding="utf-8")),
    )
    (tmp_path / "graphs").mkdir()
    return tmp_path


@pytest.fixture
def author(workspace: Path) -> Author:
    return Author(catalog=Catalog.under(workspace))


# --- 검증 없이 쓰지 않는다 ------------------------------------------------------------


def test_a_valid_graph_is_written_as_readable_yaml(author, workspace):
    path = author.save_graph("pipeline", graph("pipeline"))

    assert path == workspace / "graphs" / "pipeline.yaml"
    text = path.read_text(encoding="utf-8")
    assert list(yaml.safe_load(text)) == ["apiVersion", "kind", "metadata", "spec"]
    assert Catalog.under(workspace).graph("pipeline") == graph("pipeline")


def test_a_graph_that_fails_validation_never_touches_disk(author, workspace):
    """검증 실패한 초안은 파일이 생기지 않는다 — 이것이 이 API 의 요점이다."""
    with pytest.raises(MalkuthError) as excinfo:
        author.save_graph("bad", graph("bad", agent_name="nobody"))

    assert excinfo.value.code == ErrorCode.VAL_001
    assert not (workspace / "graphs" / "bad.yaml").exists()
    assert any("nobody" in str(f) for f in excinfo.value.details["findings"])


def test_validate_reports_findings_without_saving(author, workspace):
    report = author.validate(graphs=[graph("draft", agent_name="nobody")])

    assert not report.ok
    assert not (workspace / "graphs" / "draft.yaml").exists()


def test_a_draft_graph_can_reference_a_saved_agent(author):
    """저장된 것 + 초안을 함께 검증한다 — 초안 그래프가 기존 에이전트를 참조하는 것이 보통이다."""
    assert author.validate(graphs=[graph("draft")]).ok


def test_a_draft_agent_is_checked_even_when_no_graph_references_it(author):
    report = author.validate(agents=[agent("orphan", promptset_ref="promptsets/absent@9.9.9")])

    assert not report.ok


def test_a_draft_agent_that_breaks_a_saved_graph_is_rejected(author):
    """에이전트 초안이 기존 그래프를 깨뜨릴 수 있다 — 함께 본다."""
    author.save_graph("pipeline", graph("pipeline"))

    report = author.validate(agents=[agent("alpha", "0.2.0")])

    # pipeline 은 agents/alpha@0.1.0 을 참조한다 — 초안이 0.2.0 으로 덮으면 그 ref 가 깨진다
    assert not report.ok


# --- 버전 규칙 ---------------------------------------------------------------------


def test_saving_the_same_content_again_is_idempotent(author):
    first = author.save_graph("pipeline", graph("pipeline"))
    before = first.read_text(encoding="utf-8")

    second = author.save_graph("pipeline", graph("pipeline"))

    assert second == first and second.read_text(encoding="utf-8") == before


def test_a_changed_graph_without_a_bump_is_rejected(author, workspace):
    author.save_graph("pipeline", graph("pipeline"))
    changed = graph("pipeline").model_copy(
        update={"spec": graph("pipeline").spec.model_copy(update={"goal": "other"})}
    )

    with pytest.raises(MalkuthError) as excinfo:
        author.save_graph("pipeline", changed)

    assert excinfo.value.code == ErrorCode.MOD_002
    assert (
        yaml.safe_load((workspace / "graphs" / "pipeline.yaml").read_text())["spec"]["goal"]
        == "test"
    )


def test_a_bumped_graph_is_written(author, workspace):
    author.save_graph("pipeline", graph("pipeline"))

    author.save_graph("pipeline", graph("pipeline", "1.1.0"))

    assert Catalog.under(workspace).graph("pipeline").metadata.version == "1.1.0"


def test_a_downgrade_is_rejected(author):
    author.save_graph("pipeline", graph("pipeline", "1.1.0"))

    with pytest.raises(MalkuthError) as excinfo:
        author.save_graph("pipeline", graph("pipeline", "1.0.0"))

    assert excinfo.value.code == ErrorCode.MOD_002


def test_an_agent_bump_follows_the_same_rule(author, workspace):
    with pytest.raises(MalkuthError) as excinfo:
        author.save_agent(
            "alpha",
            agent("alpha", "0.1.0", promptset_ref="promptsets/solo@0.1.0").model_copy(
                update={
                    "metadata": agent("alpha").metadata.model_copy(
                        update={"description": "changed"}
                    )
                }
            ),
        )

    assert excinfo.value.code == ErrorCode.MOD_002


# --- 이름은 위치다 ---------------------------------------------------------------


def test_the_path_name_must_match_the_declaration(author, workspace):
    with pytest.raises(MalkuthError) as excinfo:
        author.save_graph("one", graph("two"))

    assert excinfo.value.code == ErrorCode.VAL_002
    assert not (workspace / "graphs" / "one.yaml").exists()
    assert not (workspace / "graphs" / "two.yaml").exists()


# --- 삭제 안전 ---------------------------------------------------------------------


def test_deleting_a_referenced_agent_is_refused(author, workspace):
    author.save_graph("pipeline", graph("pipeline"))

    with pytest.raises(MalkuthError) as excinfo:
        author.delete_agent("alpha")

    assert excinfo.value.code == ErrorCode.VAL_002
    assert excinfo.value.details["referenced_by"] == ["pipeline"]
    assert (workspace / "agents" / "alpha" / "manifest.yaml").exists()


def test_an_unreferenced_agent_can_be_deleted(author, workspace):
    author.delete_agent("alpha")

    assert not (workspace / "agents" / "alpha" / "manifest.yaml").exists()


def test_deleting_a_missing_graph_is_not_found(author):
    with pytest.raises(MalkuthError) as excinfo:
        author.delete_graph("nope")

    assert excinfo.value.code == ErrorCode.NF_001


def test_a_deployed_declaration_cannot_be_deleted_or_overwritten(workspace):
    """배포 중인 것을 바꾸면 실행 중 run 의 계약이 발밑에서 바뀐다 (#243 이 채운다)."""
    author = Author(catalog=Catalog.under(workspace), in_use=lambda kind, name: name == "alpha")

    with pytest.raises(MalkuthError) as refused_delete:
        author.delete_agent("alpha")
    with pytest.raises(MalkuthError) as refused_save:
        author.save_agent("alpha", agent("alpha", "0.2.0"))

    assert "deployed" in refused_delete.value.message
    assert "deployed" in refused_save.value.message


# --- 모듈은 쓰지 않는다 ------------------------------------------------------------


def test_the_author_has_no_module_writer():
    """04 Registry 2 — 게시된 모듈은 불변. 쓰는 메서드가 생기면 그것이 위반이다."""
    writers = [n for n in dir(Author) if n.startswith(("save_", "delete_"))]

    assert sorted(writers) == ["delete_agent", "delete_graph", "save_agent", "save_graph"]


# --- 깨진 파일 -----------------------------------------------------------------------


def test_a_broken_saved_file_can_be_repaired(author, workspace):
    """버전을 비교할 대상이 없는 깨진 파일은 덮어쓸 수 있어야 고치는 길이 있다."""
    (workspace / "graphs" / "pipeline.yaml").write_text("kind: Graph\n", encoding="utf-8")

    author.save_graph("pipeline", graph("pipeline"))

    assert Catalog.under(workspace).graph("pipeline") == graph("pipeline")


def test_unrelated_broken_declarations_block_saving(author, workspace):
    """읽지 못한 선언은 검증에서 빠진 것이지 통과한 것이 아니다."""
    (workspace / "agents" / "ghost").mkdir()
    (workspace / "agents" / "ghost" / "manifest.yaml").write_text("kind: Agent\n", encoding="utf-8")

    with pytest.raises(MalkuthError) as excinfo:
        author.save_graph("pipeline", graph("pipeline"))

    assert any(f.get("check") == "catalog" for f in excinfo.value.details["findings"])
