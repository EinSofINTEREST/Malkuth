"""The Control Plane must be servable as a process.

`create_app` 은 구현돼 있었지만 **그것을 만드는 곳이 테스트뿐이었다** —
`run-list` / `run-drain` / `run-resume` 세 명령이 붙을 서버가 없었다 (#221).

여기서는 진입점이 설정을 어떻게 읽는지와, run 을 **기록하지 않으면** 그 표면이
빈 목록을 돌려준다는 사실을 못 박는다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from malkuth.cli.main import build_parser, cmd_run, run_manager_for
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.orchestrator import __main__ as entrypoint
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import SqliteRunStore


def write_config(directory, orchestrator: dict) -> None:
    (directory / "local.yaml").write_text(
        yaml.safe_dump({"orchestrator": orchestrator}), encoding="utf-8"
    )


def args_for(config_dir):
    return argparse.Namespace(environment="local", config_dir=str(config_dir))


def test_serving_without_a_store_is_refused(tmp_path, monkeypatch):
    """빈 목록은 "run 이 없다" 로 읽힌다 — 설정 누락과 구분되지 않는다."""
    write_config(tmp_path, {"control_port": 18999})
    monkeypatch.setenv("MALKUTH_ENV", "local")
    monkeypatch.setenv("MALKUTH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)
    # 거절이 사라지면 진짜 서버가 떠서 테스트가 **멈춘다** — 실패는 소리나야 한다
    served: list[object] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kwargs: served.append(app))

    with pytest.raises(MalkuthError) as excinfo:
        entrypoint.main()

    assert excinfo.value.code == ErrorCode.CFG_001
    assert "run_store" in excinfo.value.message
    assert not served, "저장소 없이 서빙하면 조회가 조용히 비어 보인다"


def test_the_configured_store_is_served(tmp_path, monkeypatch):
    """설정의 저장소를 열어 서빙해야 한다 — 다른 저장소를 보면 늘 비어 있다."""
    store_path = tmp_path / "runs.db"
    write_config(tmp_path, {"run_store": str(store_path), "control_port": 18999})
    monkeypatch.setenv("MALKUTH_ENV", "local")
    monkeypatch.setenv("MALKUTH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    served: dict = {}

    def capture(app, *, host, port, log_config):  # noqa: ARG001
        served["app"] = app
        served["host"] = host
        served["port"] = port

    monkeypatch.setattr(entrypoint.uvicorn, "run", capture)
    entrypoint.main()

    assert served["port"] == 18999
    assert served["host"] == "127.0.0.1"
    assert served["app"] is not None


def test_the_served_app_refuses_resume(tmp_path):
    """이 프로세스는 run 을 구동하지 않는다 — 조용히 성공하면 운영자가 오해한다."""
    store = SqliteRunStore(path=str(tmp_path / "runs.db"))
    app = create_app(store)

    routes = {route.path for route in app.routes}  # type: ignore[attr-defined]

    assert "/v1/runs/{run_id}/resume" in routes


def test_the_cli_records_runs_when_a_store_is_configured(tmp_path):
    """#221 — 기록하지 않으면 control plane 이 떠 있어도 빈 목록을 돌려준다."""
    store_path = tmp_path / "runs.db"
    write_config(tmp_path, {"run_store": str(store_path)})

    manager = run_manager_for(args_for(tmp_path))

    assert manager.store is not None, "설정에 저장소가 있는데 manager 가 기록하지 않는다"


def test_the_cli_stays_storeless_without_configuration(tmp_path):
    """설정하지 않은 사람에게 파일을 만들지 않는다 — 기본은 그대로다."""
    write_config(tmp_path, {})

    manager = run_manager_for(args_for(tmp_path))

    assert manager.store is None


def test_the_cli_honours_the_configured_slots(tmp_path):
    """상한도 설정이 정한다 — manager 를 새로 만들면서 놓치기 쉽다."""
    write_config(tmp_path, {"max_concurrent_runs": 3, "max_service_runs": 2})

    manager = run_manager_for(args_for(tmp_path))

    assert (manager._max_mission, manager._max_service) == (3, 2)


def test_a_submitted_run_is_actually_recorded(tmp_path):
    """#221 — `run_manager_for` 가 있어도 **부르는 곳이 없으면** 기록되지 않는다.

    헬퍼만 따로 검사하면 배선이 빠져도 초록이다 — 이 프로젝트가 반복해서
    만난 함정이다. 그래서 `cmd_run` 을 실제로 태운다.

    에이전트를 주지 않아 run 은 실패하지만, **실패한 run 도 기록되어야** 한다:
    운영자가 control plane 에서 봐야 하는 것이 바로 그런 run 이다.
    """
    store_path = tmp_path / "runs.db"
    write_config(tmp_path, {"run_store": str(store_path)})
    graph = Path(__file__).resolve().parents[3] / "graphs" / "research-pipeline.yaml"

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            str(graph),
            "--input",
            '{"query": "q"}',
            "--run-id",
            "recorded-run",
            "--env",
            "local",
            "--config-dir",
            str(tmp_path),
        ]
    )
    cmd_run(args)

    recorded = SqliteRunStore(path=str(store_path)).get("recorded-run")
    assert recorded is not None, "제출한 run 이 저장소에 남지 않았다"
    assert recorded.graph == "research-pipeline"
