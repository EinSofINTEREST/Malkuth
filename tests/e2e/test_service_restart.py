"""Service runs across a process restart.

service 루프(idle backoff / iteration checkpoint / drain / `GRAPH_005`)는 fake
clock 으로 유닛에서 증명된다. 증명되지 않은 것은 **프로세스가 죽었다 살아난
뒤 이어지는가**였다 (#163).

in-memory checkpointer 로는 넘길 수 없다 — 프로세스 밖에 state 가 있어야
한다. 그래서 스택에 Postgres 를 띄우고, 그것을 상대로 검증한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from malkuth.orchestrator.checkpoint import build_checkpointer, close_checkpointer
from malkuth.orchestrator.run import RunManager
from malkuth.orchestrator.runstore import SqliteRunStore
from malkuth.orchestrator.submit import RunSubmitter
from malkuth.orchestrator.topology import GraphTopology
from malkuth.runtime.control import ControlClient
from malkuth.runtime.nodes import ControlNodeRuntime
from tests.e2e.test_stack import (
    AGENT_TOKEN,
    COMPOSE_FILE,
    compose_up,
    docker,
    requires_docker,
    wait_healthy,
)

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PORTS = {"watcher": 18083, "classifier": 18082, "notifier": 18084}
CHECKPOINT_URL = "postgresql://malkuth:malkuth@127.0.0.1:15433/malkuth"


def topology() -> GraphTopology:
    """저장소의 레퍼런스 service 그래프."""
    return GraphTopology.model_validate(
        yaml.safe_load((REPO_ROOT / "graphs" / "feed-monitor.yaml").read_text("utf-8"))
    )


@pytest.fixture(scope="module")
def service_stack() -> Iterator[dict[str, int]]:
    """세 에이전트 + 외부 checkpointer — finalizer 가 반드시 정리한다."""
    compose_up()
    try:
        for port in AGENT_PORTS.values():
            assert wait_healthy(f"http://127.0.0.1:{port}"), f"agent on {port} never became healthy"
        yield AGENT_PORTS
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


@pytest.fixture
async def process(service_stack: dict[str, int], tmp_path):
    """한 오케스트레이터 '프로세스' 를 만든다.

    같은 저장소 파일과 같은 checkpointer 를 여는 **새 객체**가 곧 재시작이다 —
    프로세스를 실제로 죽이지 않아도 그 경계가 재현된다 (#186 이 그것을
    가능하게 했다).
    """
    store_path = str(tmp_path / "runs.db")
    opened: list[Any] = []

    async def spawn() -> RunSubmitter:
        clients = {
            name: ControlClient(f"http://127.0.0.1:{port}", agent=name, token=AGENT_TOKEN)
            for name, port in service_stack.items()
        }
        checkpointer = build_checkpointer("postgres", url=CHECKPOINT_URL)
        submitter = RunSubmitter(
            runtime=ControlNodeRuntime(clients=clients),
            checkpointer=checkpointer,
            manager=RunManager(store=SqliteRunStore(path=store_path)),
        )
        opened.append((clients, checkpointer))
        return submitter

    try:
        yield spawn
    finally:
        for clients, checkpointer in opened:
            for client in clients.values():
                await client.aclose()
            await close_checkpointer(checkpointer)


async def no_sleep(_delay: float) -> None:
    """idle backoff 를 즉시 통과시킨다 — 06 은 실제 sleep 을 금지한다."""


@requires_docker
async def test_a_service_run_iterates_over_the_live_stack(process):
    """재시작을 보기 전에, 실제 컨테이너로 iteration 이 도는지부터 확인한다."""
    submitter = await process()

    handle = await submitter.start_service(
        topology(), {}, run_id="e2e-service", max_iterations=2, sleep=no_sleep
    )
    await submitter.services[handle.run_id]

    assert handle.iteration == 2


def iteration_thread(run_id: str, iteration: int) -> dict[str, Any]:
    """service run 의 checkpoint thread — **iteration 마다 별개**다.

    01 은 iteration 단위 checkpoint 를 규정한다: 한 thread 에 누적하면 재개
    지점이 회차 경계와 어긋난다.
    """
    return {"configurable": {"thread_id": f"{run_id}:{iteration}"}}


@requires_docker
async def test_the_checkpoint_outlives_the_process(process):
    """in-memory 로는 넘길 수 없다 — 프로세스 밖에 state 가 있어야 한다."""
    first = await process()
    handle = await first.start_service(
        topology(), {}, run_id="e2e-outlive", max_iterations=1, sleep=no_sleep
    )
    await first.services[handle.run_id]

    # 재시작 — 같은 저장소를 여는 새 checkpointer
    second = await process()
    found = await second.checkpointer.aget_tuple(  # type: ignore[union-attr]
        iteration_thread("e2e-outlive", 0)
    )

    assert found is not None


@requires_docker
async def test_a_halted_run_is_visible_after_the_restart(process):
    """새 프로세스가 run 을 못 찾으면 재개 자체가 불가능하다 (#186)."""
    first = await process()
    handle = await first.start_service(
        topology(), {}, run_id="e2e-visible", max_iterations=1, sleep=no_sleep
    )
    await first.services[handle.run_id]

    second = await process()
    restored = second.manager.get("e2e-visible")

    assert restored.run_id == "e2e-visible"
    assert restored.iteration == handle.iteration


@requires_docker
async def test_the_restarted_process_does_not_replay_finished_iterations(process):
    """실패한 회차를 다시 돌리면 부수효과가 겹친다 (01 Mode Rules 2)."""
    first = await process()
    handle = await first.start_service(
        topology(), {}, run_id="e2e-noreplay", max_iterations=3, sleep=no_sleep
    )
    await first.services[handle.run_id]
    completed = handle.iteration

    second = await process()
    restored = second.manager.get("e2e-noreplay")

    assert restored.iteration == completed
    assert completed == 3


@requires_docker
async def test_a_drain_request_survives_the_restart(process):
    """정지 요청이 재시작에서 지워지면 운영자가 다시 눌러야 한다."""
    first = await process()
    handle = await first.start_service(
        topology(), {}, run_id="e2e-drain", max_iterations=1, sleep=no_sleep
    )
    await first.services[handle.run_id]
    store = first.manager.store
    assert store is not None
    store.request_drain("e2e-drain")

    second = await process()

    assert second.manager.get("e2e-drain").drain_requested


@requires_docker
async def test_two_runs_keep_separate_checkpoints(process):
    """thread 가 섞이면 한 run 의 재개가 다른 run 의 state 를 집는다."""
    submitter = await process()
    for run_id in ("e2e-sep-a", "e2e-sep-b"):
        handle = await submitter.start_service(
            topology(), {}, run_id=run_id, max_iterations=1, sleep=no_sleep
        )
        await submitter.services[handle.run_id]

    checkpointer = submitter.checkpointer
    assert checkpointer is not None
    first = await checkpointer.aget_tuple(iteration_thread("e2e-sep-a", 0))
    second = await checkpointer.aget_tuple(iteration_thread("e2e-sep-b", 0))

    assert first is not None
    assert second is not None
    assert first.config["configurable"]["thread_id"] != second.config["configurable"]["thread_id"]


@requires_docker
def test_the_checkpoint_store_is_declared_in_the_stack():
    """스택에 외부 checkpointer 가 없으면 이 파일의 모든 검증이 무의미하다."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text("utf-8"))

    assert "checkpoint-db" in compose["services"]
    volumes = compose["services"]["checkpoint-db"]["volumes"]
    assert any("checkpoint-data" in str(entry) for entry in volumes)
