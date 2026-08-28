"""Resuming a run this process did not start.

`RunStore` 의 docstring 은 "Where run records live so other processes can reach
them" 이라고 말하는데, **읽는 쪽이 없었다** — `RunManager.get` 이 in-process
dict 만 봐서 재시작을 넘는 재개가 `NF_001` 로 실패했다 (#186).

01 은 "프로세스/호스트 재시작 시 마지막 iteration 에서 재개" 를 규정한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from malkuth.core.agent import TaskResult
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.orchestrator.checkpoint import build_checkpointer
from malkuth.orchestrator.run import GraphMode, RunManager, RunStatus
from malkuth.orchestrator.runstore import RunRecord, SqliteRunStore
from malkuth.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_service


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        self.invoked.append(task.run_id)
        return TaskResult.completed(task, output={})


async def no_sleep(_delay: float) -> None:
    """idle backoff 를 즉시 통과시킨다 — 06 은 실제 sleep 을 금지한다."""


@pytest.fixture
def store_path(tmp_path):
    """두 프로세스가 공유하는 저장소 파일."""
    return str(tmp_path / "runs.db")


def halted_record(run_id: str = "svc", *, iteration: int = 5) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        graph="feed-monitor",
        mode="service",
        status=str(RunStatus.HALTED),
        iteration=iteration,
    )


def restarted(store_path: str) -> RunSubmitter:
    """재시작된 프로세스 — 같은 저장소를 여는 **새** 매니저."""
    return RunSubmitter(
        runtime=EchoRuntime(),
        manager=RunManager(store=SqliteRunStore(path=store_path)),
        checkpointer=build_checkpointer("memory"),
    )


def test_a_run_from_another_process_is_found(store_path):
    """#186 — 이 조회가 없어 재시작을 넘는 재개가 전부 NF_001 이었다."""
    SqliteRunStore(path=store_path).upsert(halted_record())

    handle = restarted(store_path).manager.get("svc")

    assert handle.run_id == "svc"
    assert handle.status is RunStatus.HALTED
    assert handle.iteration == 5


def test_an_unknown_run_still_fails(store_path):
    """저장소를 봐도 없으면 없는 것이다 — 조용히 빈 핸들을 만들면 안 된다."""
    with pytest.raises(MalkuthError) as excinfo:
        restarted(store_path).manager.get("absent")

    assert excinfo.value.code == ErrorCode.NF_001


def test_a_restored_run_does_not_consume_a_slot(store_path):
    """죽은 프로세스의 run 이 active 로 잡히면 동시 실행 상한을 잠식한다."""
    SqliteRunStore(path=store_path).upsert(
        RunRecord(
            run_id="live", graph="feed-monitor", mode="service", status=str(RunStatus.RUNNING)
        )
    )
    manager = restarted(store_path).manager

    manager.get("live")

    assert manager.active(GraphMode.SERVICE) == 0


def test_a_drain_request_survives_the_restart(store_path):
    """정지 요청이 재시작에서 지워지면 운영자가 다시 눌러야 한다."""
    store = SqliteRunStore(path=store_path)
    store.upsert(halted_record("draining"))
    store.request_drain("draining")

    handle = restarted(store_path).manager.get("draining")

    assert handle.drain_requested


def test_manager_without_a_store_is_unchanged(tmp_path):
    """저장소를 안 물린 배선은 그대로 동작해야 한다."""
    with pytest.raises(MalkuthError) as excinfo:
        RunManager().get("svc")

    assert excinfo.value.code == ErrorCode.NF_001


async def test_service_resume_continues_past_the_restart(store_path):
    """01 — 재시작 뒤 마지막 iteration **다음**부터 이어간다."""
    SqliteRunStore(path=store_path).upsert(halted_record(iteration=5))
    submitter = restarted(store_path)

    handle = await submitter.resume_service(make_service(), "svc", max_iterations=8, sleep=no_sleep)
    await submitter.services[handle.run_id]

    assert handle.iteration == 8
    # 실패한 회차를 다시 돌리면 부수효과가 겹친다
    assert handle.iteration > 5


async def test_resume_still_refuses_a_run_that_is_not_halted(store_path):
    """살아있는 run 을 재개하면 같은 iteration 을 두 벌이 돈다."""
    SqliteRunStore(path=store_path).upsert(
        RunRecord(
            run_id="live", graph="feed-monitor", mode="service", status=str(RunStatus.RUNNING)
        )
    )

    with pytest.raises(MalkuthError) as excinfo:
        await restarted(store_path).resume_service(make_service(), "live", max_iterations=1)

    assert excinfo.value.code == ErrorCode.GRAPH_001
