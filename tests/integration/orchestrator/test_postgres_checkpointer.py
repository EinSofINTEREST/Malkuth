"""Postgres checkpointer against a real server.

이 분기는 **한 번도 실행된 적이 없었다** — 선택 의존성이라 패키지가 없으면
`CFG_001` 로 거부됐고, 유닛은 그 거부 경로만 덮었다. 패키지를 넣자마자 두
결함이 드러났다 (#171):

1. `from_conn_string` 은 **context manager** 다 — 반환값을 checkpointer 로
   쓰면 그래프에 물리는 순간 깨진다
2. 동기 `PostgresSaver` 는 `aget_tuple` 이 `NotImplementedError` 다 —
   오케스트레이터는 `ainvoke` 로 굴린다
"""

from __future__ import annotations

import os

import pytest

from malkuth.core.agent import TaskResult
from malkuth.orchestrator.checkpoint import build_checkpointer, close_checkpointer
from malkuth.orchestrator.run import RunStatus
from malkuth.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_mission

pytestmark = pytest.mark.integration

URL = os.environ.get("MALKUTH_TEST_POSTGRES_URL", "")
requires_postgres = pytest.mark.skipif(not URL, reason="MALKUTH_TEST_POSTGRES_URL not set")


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node, task) -> TaskResult:
        return TaskResult.completed(task, output={})


@requires_postgres
async def test_a_graph_run_completes_with_the_postgres_checkpointer():
    """#171 의 핵심 — 반환된 객체가 실제로 checkpointer 여야 한다."""
    checkpointer = build_checkpointer("postgres", url=URL)
    submitter = RunSubmitter(runtime=EchoRuntime(), checkpointer=checkpointer)

    result = await submitter.submit(make_mission(), {"query": "q"}, run_id="pg-complete")

    assert result.status is RunStatus.COMPLETED


@requires_postgres
async def test_the_checkpoint_is_readable_after_the_run():
    """저장만 되고 읽히지 않으면 재개가 불가능하다."""
    checkpointer = build_checkpointer("postgres", url=URL)
    submitter = RunSubmitter(runtime=EchoRuntime(), checkpointer=checkpointer)
    await submitter.submit(make_mission(), {"query": "q"}, run_id="pg-readable")

    found = await checkpointer.aget_tuple({"configurable": {"thread_id": "pg-readable"}})

    assert found is not None


@requires_postgres
async def test_the_async_contract_is_served():
    """동기 saver 를 돌려주면 ainvoke 가 NotImplementedError 로 죽는다."""
    checkpointer = build_checkpointer("postgres", url=URL)

    # 준비 전에도 비동기 계약이 있어야 한다 — 그래프가 그것으로 물린다
    assert hasattr(checkpointer, "aget_tuple")
    assert await checkpointer.aget_tuple({"configurable": {"thread_id": "absent"}}) is None


@requires_postgres
async def test_two_runs_do_not_share_a_thread():
    """thread 가 섞이면 한 run 의 재개가 다른 run 의 state 를 집는다."""
    checkpointer = build_checkpointer("postgres", url=URL)
    submitter = RunSubmitter(runtime=EchoRuntime(), checkpointer=checkpointer)
    await submitter.submit(make_mission(), {"query": "a"}, run_id="pg-a")
    await submitter.submit(make_mission(), {"query": "b"}, run_id="pg-b")

    first = await checkpointer.aget_tuple({"configurable": {"thread_id": "pg-a"}})
    second = await checkpointer.aget_tuple({"configurable": {"thread_id": "pg-b"}})

    assert first is not None
    assert second is not None
    assert first.config["configurable"]["thread_id"] != second.config["configurable"]["thread_id"]


# --- 커넥션 수명 --------------------------------------------------------------
# 여는 코드만 있고 닫는 코드가 없으면 "소유자가 명확하다" 고 볼 수 없다


@requires_postgres
async def test_closing_releases_the_connection():
    """상주 프로세스는 프로세스 종료에 기댈 수 없다 — 물고 있으면 샌다."""
    checkpointer = build_checkpointer("postgres", url=URL)
    await checkpointer.aget_tuple({"configurable": {"thread_id": "pg-close"}})
    assert checkpointer.conn is not None

    await close_checkpointer(checkpointer)

    assert checkpointer.conn.closed


@requires_postgres
async def test_closing_an_unused_checkpointer_is_harmless():
    """한 번도 쓰이지 않았으면 열린 것이 없다 — 정리가 실패하면 안 된다."""
    await close_checkpointer(build_checkpointer("postgres", url=URL))


@requires_postgres
async def test_reuse_after_close_reopens():
    """닫은 뒤 다시 쓰면 살아나야 한다 — 아니면 정리가 곧 파괴가 된다."""
    checkpointer = build_checkpointer("postgres", url=URL)
    await checkpointer.aget_tuple({"configurable": {"thread_id": "pg-reopen"}})
    await close_checkpointer(checkpointer)

    assert await checkpointer.aget_tuple({"configurable": {"thread_id": "pg-reopen"}}) is None
    assert not checkpointer.conn.closed
    await close_checkpointer(checkpointer)


async def test_closing_an_in_memory_checkpointer_is_a_noop():
    """호출자는 어느 백엔드인지 알 필요가 없어야 한다."""
    await close_checkpointer(build_checkpointer("memory"))
