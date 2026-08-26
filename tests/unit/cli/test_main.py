"""Smoke tests for the malkuth CLI.

실제 컨테이너 없이 명령 표면을 검증한다 — 이 계층의 계약은 인자 해석과
종료 코드지 오케스트레이션이 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.cli.main import (
    EXIT_FAILED,
    EXIT_OK,
    build_parser,
    discover_refs,
    main,
    validate_root,
)
from malkuth.orchestrator.topology import GraphTopology

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """유효한 최소 저장소 — 배포 검증을 통과하는 구성."""
    (tmp_path / "agents" / "solo").mkdir(parents=True)
    (tmp_path / "groups").mkdir()
    (tmp_path / "graphs").mkdir()
    (tmp_path / "modules" / "promptsets" / "solo" / "0.1.0").mkdir(parents=True)

    (tmp_path / "agents" / "solo" / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Agent",
                "metadata": {"name": "solo", "version": "0.1.0"},
                "spec": {
                    "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                    "promptset": {"ref": "promptsets/solo@0.1.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "groups" / "global.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Group",
                "metadata": {"name": "global", "version": "0.1.0"},
                "spec": {"secrets": ["ANTHROPIC_API_KEY"]},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "graphs" / "solo-graph.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Graph",
                "metadata": {"name": "solo-graph", "version": "1.0.0"},
                "spec": {
                    "mode": "mission",
                    "goal": "단일 노드 실행",
                    "state": {"schema": "malkuth.graphs.schemas:ResearchState"},
                    "nodes": [{"id": "solo", "agent": "agents/solo@0.1.0"}],
                    "edges": [
                        {"from": "START", "to": "solo"},
                        {"from": "solo", "to": "END"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def run_cli(argv: list[str]) -> int:
    return main(argv)


# --- 파서 ---------------------------------------------------------------------


def test_parser_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


@pytest.mark.parametrize("command", ["deploy", "validate", "status", "config", "check"])
def test_every_command_is_registered(command):
    assert command in build_parser().format_help()


def test_port_range_is_parsed():
    args = build_parser().parse_args(["validate", "--a2a-port-range", "9100-9199"])

    assert args.a2a_port_range == (9100, 9199)


# --- status -------------------------------------------------------------------


def test_status_lists_declared_artifacts(workspace, capsys):
    assert run_cli(["--root", str(workspace), "status"]) == EXIT_OK

    output = capsys.readouterr().out
    assert "solo" in output
    assert "solo-graph" in output


def test_status_emits_json(workspace, capsys):
    import json

    run_cli(["--root", str(workspace), "--json", "status"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"] == ["solo"]


# --- validate / deploy --------------------------------------------------------


def test_validate_passes_on_a_valid_workspace(workspace, capsys):
    assert run_cli(["--root", str(workspace), "validate"]) == EXIT_OK
    assert "status: ok" in capsys.readouterr().out


def test_validate_fails_on_a_dangling_ref(workspace, capsys):
    """게시되지 않은 모듈을 참조하면 배포 전에 막혀야 한다."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["promptset"]["ref"] = "promptsets/absent@9.9.9"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    assert run_cli(["--root", str(workspace), "validate"]) == EXIT_FAILED
    assert "module_refs" in capsys.readouterr().out


def test_deploy_validates_a_single_graph(workspace, capsys):
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "deploy", graph]) == EXIT_OK


def test_deploy_reports_failures_without_starting_anything(workspace, capsys):
    """검증 실패 시 아무것도 기동하지 않는다 — 이 명령의 요점이다."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["metadata"]["group"] = "absent"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "deploy", graph]) == EXIT_FAILED
    assert "groups" in capsys.readouterr().out


# --- config -------------------------------------------------------------------


def test_config_prints_the_resolved_settings(capsys):
    assert run_cli(["config", "dev", "--config-dir", str(REPO_ROOT / "configs")]) == EXIT_OK

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["orchestrator"]["checkpointer"] == "memory"


def test_unknown_environment_reports_a_structured_error(tmp_path, capsys):
    """스택 트레이스를 그대로 뱉으면 운영자가 원인을 찾기 어렵다."""
    assert run_cli(["config", "absent", "--config-dir", str(tmp_path)]) == EXIT_FAILED
    assert "CFG_001" in capsys.readouterr().err


# --- check --------------------------------------------------------------------


def test_check_reports_no_discrepancies(tmp_path, capsys):
    state = tmp_path / "state.yaml"
    state.write_text(yaml.safe_dump({"runs": ["r1"], "checkpoints": ["r1"]}), encoding="utf-8")

    assert run_cli(["check", str(state)]) == EXIT_OK


def test_check_reports_orphans(tmp_path, capsys):
    state = tmp_path / "state.yaml"
    state.write_text(
        yaml.safe_dump({"runs": ["r1"], "checkpoints": [], "running": ["ghost"]}),
        encoding="utf-8",
    )

    assert run_cli(["check", str(state)]) == EXIT_FAILED

    output = capsys.readouterr().out
    assert "run_without_checkpoint" in output
    assert "ghost_container" in output


def test_check_rejects_a_non_mapping_state(tmp_path, capsys):
    state = tmp_path / "state.yaml"
    state.write_text("- a\n- b\n", encoding="utf-8")

    assert run_cli(["check", str(state)]) == EXIT_FAILED
    assert "CFG_001" in capsys.readouterr().err


# --- ref discovery ------------------------------------------------------------


def test_refs_are_recovered_from_the_directory_layout(workspace):
    refs = discover_refs(workspace / "modules")

    assert refs == frozenset({"promptsets/solo@0.1.0"})


def test_missing_module_root_yields_no_refs(tmp_path):
    assert discover_refs(tmp_path / "absent") == frozenset()


# --- 실제 저장소 ---------------------------------------------------------------


def test_repository_validates_end_to_end(capsys):
    """저장소 전체가 8항목을 통과해야 한다 — CLI 가 만든 예제만 통과하면
    아무것도 증명하지 못한다."""
    assert run_cli(["--root", str(REPO_ROOT), "validate"]) == EXIT_OK


# --- run ----------------------------------------------------------------------


def test_run_is_registered():
    assert "run" in build_parser().format_help()


def test_run_rejects_an_invalid_deployment(workspace, capsys):
    """검증에 실패한 그래프를 굴리면 노드 실행 중에야 실패한다."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["promptset"]["ref"] = "promptsets/absent@9.9.9"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "run", graph]) == EXIT_FAILED

    output = capsys.readouterr().out
    assert "rejected" in output
    assert "module_refs" in output


def test_run_without_agent_addresses_fails_at_the_node(workspace, capsys):
    """에이전트가 떠 있지 않으면 노드 실행이 GRAPH_002 로 실패한다."""
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "run", graph]) == EXIT_FAILED

    assert "failed" in capsys.readouterr().out


def test_run_parses_agent_addresses():
    args = build_parser().parse_args(
        ["run", "g.yaml", "--agent", "planner=http://a:8080", "--agent", "writer=http://b:8080"]
    )

    assert args.agent == ["planner=http://a:8080", "writer=http://b:8080"]


def test_run_and_validate_agree_on_the_same_repository(workspace):
    """세 명령이 같은 입력에 다른 판정을 내리면 한 명령만 통과하는 상태가 생긴다."""
    topology = GraphTopology.model_validate(
        yaml.safe_load((workspace / "graphs" / "solo-graph.yaml").read_text())
    )

    assert validate_root(workspace, [topology]).ok


def test_global_secrets_resolve_for_every_command():
    """run 이 global_secrets 를 빠뜨리면 deploy 는 통과하는 배포를 거부한다."""
    topologies = [
        GraphTopology.model_validate(
            yaml.safe_load((REPO_ROOT / "graphs" / f"{name}.yaml").read_text())
        )
        for name in ("research-pipeline",)
    ]

    assert validate_root(REPO_ROOT, topologies).ok
