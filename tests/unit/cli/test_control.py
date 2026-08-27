"""CLI control-plane command tests.

#102 의 목적은 **다른 프로세스의 run 을 조작하는 것**이다. 그래서 이 테스트는
CLI 가 자기가 띄우지 않은 run 을 다루는 경로를 검증한다.
"""

from __future__ import annotations

import time

import pytest

from malkuth.cli.control import DEFAULT_CONTROL_URL, ControlClient
from malkuth.cli.main import main
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import InMemoryRunStore, RunRecord


def record(run_id: str = "run-1", **overrides) -> RunRecord:
    base = {
        "run_id": run_id,
        "graph": "feed-monitor",
        "mode": "service",
        "status": "running",
    }
    return RunRecord(**{**base, **overrides})


@pytest.fixture
def served():
    """실제 Control Plane 을 loopback 에 띄운다.

    CLI 의 클라이언트는 **동기** httpx 를 쓴다 (CLI 는 async 가 아니다) —
    ASGITransport 는 async 전용이므로, 진짜 서버를 띄워 동기 HTTP 경로를
    그대로 태운다.
    """
    import threading

    import uvicorn

    store = InMemoryRunStore()
    config = uvicorn.Config(create_app(store), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "control plane did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield store, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# --- 클라이언트 계약 --------------------------------------------------------------


def test_the_client_lists_runs(served):
    store, url = served
    store.upsert(record())

    listed = ControlClient(url).list_runs()

    assert [item["run_id"] for item in listed] == ["run-1"]


def test_the_client_narrows_by_mode(served):
    store, url = served
    store.upsert(record("run-m", mode="mission"))
    store.upsert(record("run-s", mode="service"))

    listed = ControlClient(url).list_runs(mode="mission")

    assert [item["run_id"] for item in listed] == ["run-m"]


def test_the_client_requests_a_drain(served):
    store, url = served
    store.upsert(record())

    result = ControlClient(url).drain("run-1")

    assert result["drain_requested"] is True
    stored = store.get("run-1")
    assert stored is not None
    assert stored.drain is True


def test_an_unknown_run_is_reported_as_not_found(served):
    _store, url = served
    with pytest.raises(MalkuthError) as exc_info:
        ControlClient(url).drain("never-existed")

    assert exc_info.value.code == ErrorCode.NF_001


def test_an_unreachable_control_plane_is_not_a_raw_connection_error():
    """운영자가 보는 것이 ConnectionRefusedError 면 원인을 찾기 어렵다."""
    with pytest.raises(MalkuthError) as exc_info:
        ControlClient("http://127.0.0.1:1", timeout_s=0.2).list_runs()

    assert exc_info.value.code == ErrorCode.NET_001
    assert "unreachable" in exc_info.value.message
    assert exc_info.value.retryable


# --- CLI 표면 -----------------------------------------------------------------


def test_run_list_prints_the_runs(served, capsys):
    store, url = served
    store.upsert(record())

    code = main(["--json", "run-list", "--control-url", url])

    assert code == 0
    assert "run-1" in capsys.readouterr().out


def test_run_drain_reports_that_the_stop_is_deferred(served, capsys):
    """즉시 정지로 오해하면 운영자가 곧바로 다음 조치를 한다."""
    store, url = served
    store.upsert(record())

    code = main(["run-drain", "run-1", "--control-url", url])

    assert code == 0
    printed = capsys.readouterr().out
    assert "current iteration" in printed


def test_run_drain_on_an_unknown_run_exits_nonzero(served, capsys):
    """0 을 돌려주면 운영 스크립트가 조작이 먹혔다고 믿고 넘어간다."""
    _store, url = served
    code = main(["run-drain", "never-existed", "--control-url", url])

    assert code == 1
    assert "NF_001" in capsys.readouterr().out


def test_an_unreachable_control_plane_exits_nonzero_with_guidance(capsys):
    code = main(["run-list", "--control-url", "http://127.0.0.1:1"])

    assert code == 1
    printed = capsys.readouterr().out
    assert "NET_001" in printed
    assert "unreachable" in printed


def test_resume_without_a_driver_is_reported_as_failure(served, capsys):
    """501 을 성공으로 읽으면 운영자가 재개됐다고 믿고 손을 뗀다."""
    store, url = served
    store.upsert(record(status="halted"))

    code = main(["run-resume", "run-1", "--control-url", url])

    assert code == 1


# --- 기존 명령 보존 --------------------------------------------------------------


def test_the_existing_run_command_still_takes_a_graph():
    """`run` 아래 subparser 로 넣었다면 이 형태가 깨진다."""
    from malkuth.cli.main import build_parser

    parsed = build_parser().parse_args(["run", "graphs/feed-monitor.yaml"])

    assert parsed.graph == "graphs/feed-monitor.yaml"


def test_the_default_control_url_is_used_when_unspecified():
    from malkuth.cli.main import build_parser

    parsed = build_parser().parse_args(["run-list"])

    assert parsed.control_url is None
    assert DEFAULT_CONTROL_URL.startswith("http")


# --- 프로세스 경계 (#125 의 존재 이유) ---------------------------------------------


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node, task):
        from malkuth.core.agent import TaskResult

        return TaskResult.completed(task, output={})


async def _no_wait(_seconds: float) -> None:
    return None


def test_a_cli_drain_stops_a_run_started_elsewhere(tmp_path):
    """#125 완료 조건 — 프로세스 A 의 run 을 B 의 CLI 로 멈춘다.

    A(구동)와 B(CLI)는 **별개의 저장소 연결**을 쓴다. 하나를 공유하면 경계를
    넘는지 알 수 없다.
    """
    import asyncio
    import threading

    import uvicorn

    from malkuth.orchestrator.builder import build_graph
    from malkuth.orchestrator.run import RunManager, RunStatus, ServiceRunner
    from malkuth.orchestrator.runstore import SqliteRunStore
    from tests.fixtures.topologies import make_service

    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)  # 프로세스 A — run 을 구동한다
    serving = SqliteRunStore(path=path)  # Control Plane 이 보는 창구
    config = uvicorn.Config(create_app(serving), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]

        topology = make_service()
        handle = RunManager(store=driver).acquire(topology)
        runner = ServiceRunner(
            topology, build_graph(topology, EchoRuntime()), sleep=_no_wait, store=driver
        )

        # 프로세스 B — CLI 로 drain 을 요청한다
        code = main(["run-drain", handle.run_id, "--control-url", f"http://127.0.0.1:{port}"])
        assert code == 0

        # 프로세스 A 의 루프가 그것을 읽고 멈춘다
        asyncio.run(runner.run(handle, {"feeds": []}, max_iterations=50))

        assert handle.status is RunStatus.STOPPED
        assert handle.iteration == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        driver.close()
        serving.close()


def test_a_cli_list_shows_a_run_started_elsewhere(tmp_path):
    """운영자가 자기가 띄우지 않은 run 을 볼 수 없으면 조작할 대상도 모른다."""
    import threading

    import uvicorn

    from malkuth.orchestrator.runstore import SqliteRunStore

    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    serving = SqliteRunStore(path=path)
    config = uvicorn.Config(create_app(serving), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]

        driver.upsert(record("run-elsewhere", mode="service"))

        listed = ControlClient(f"http://127.0.0.1:{port}").list_runs()

        assert [item["run_id"] for item in listed] == ["run-elsewhere"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        driver.close()
        serving.close()
