"""Memory access wiring tests.

토큰은 **선언으로부터 조립**되어야 한다 — 토큰을 직접 만들어 검증하면
조립 경로의 결함을 놓친다. 접근 경계는 선언 위치가 정한다 (09).
"""

from __future__ import annotations

import pytest

from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest, GroupManifest, MemorySpec
from malkuth.memory.entry import MemoryEntry, MemorySource
from malkuth.memory.index import IndexRegistry, SpaceIndex
from malkuth.memory.recall import Recall
from malkuth.memory.service import MemoryService
from malkuth.memory.store import SqliteMemoryStore
from malkuth.modules.memoryset import ChunkSpec, MemoryKind
from malkuth.runtime.memory import ServiceMemoryAccess, issue_token
from tests.fixtures.builders import manifest_dict


def manifest(*, group_name: str | None = None, **memory: object) -> AgentManifest:
    """local space 를 선언한 매니페스트 — 그룹 소속은 metadata 가 정한다."""
    spec = manifest_dict()
    if memory:
        spec["spec"]["memory"] = memory
    if group_name is not None:
        spec["metadata"]["group"] = group_name
    return AgentManifest.model_validate(spec)


def group(name: str, spaces: list[dict[str, object]]) -> GroupManifest:
    return GroupManifest.model_validate(
        {
            "apiVersion": "malkuth/v1",
            "kind": "Group",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {"memory": {"spaces": spaces}},
        }
    )


LOCAL = {"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]}
GROUP_SPACES = [{"ref": "memorysets/domain-knowledge@0.1.0", "as": "knowledge", "mode": "rw"}]


# --- 토큰 조립 ----------------------------------------------------------------


def test_local_spaces_come_from_the_manifest():
    token = issue_token(manifest(**LOCAL))

    assert token.resolve("longterm") is not None


def test_undeclared_space_is_absent_from_the_token():
    """선언되지 않은 space 는 존재조차 알려주지 않는다."""
    token = issue_token(manifest(**LOCAL))

    assert token.resolve("secret") is None


def test_group_spaces_reach_members():
    token = issue_token(
        manifest(group_name="research", **LOCAL), group=group("research", GROUP_SPACES)
    )

    assert token.resolve("knowledge") is not None


def test_group_spaces_do_not_reach_non_members():
    """비멤버는 그룹 선언을 받아도 닿을 수 없다 — 소속이 곧 경계다.

    그룹을 넘기지 않고 확인하면 "안 넘겨서 없다" 를 검증하는 셈이라 무의미하다.
    """
    token = issue_token(manifest(**LOCAL), group=group("research", GROUP_SPACES))

    assert token.resolve("knowledge") is None


def test_direct_tasks_get_no_run_scope():
    """그래프 run 과 무관한 태스크가 run 의 기억을 건드리면 격리가 무너진다."""
    run_spaces = MemorySpec.model_validate(
        {"spaces": [{"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}]}
    )

    token = issue_token(manifest(**LOCAL), run_id=None, run_spaces=run_spaces)

    assert token.resolve("scratch") is None


def test_graph_tasks_get_the_run_scope():
    run_spaces = MemorySpec.model_validate(
        {"spaces": [{"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}]}
    )

    token = issue_token(manifest(**LOCAL), run_id="run-1", run_spaces=run_spaces)

    space = token.resolve("scratch")
    assert space is not None
    assert "run-1" in space.space_id


def test_local_alias_wins_over_group():
    """별칭이 겹치면 가까운 스코프가 이긴다 (local > group > global)."""
    collide = {"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "knowledge"}]}

    token = issue_token(
        manifest(group_name="research", **collide), group=group("research", GROUP_SPACES)
    )

    space = token.resolve("knowledge")
    assert space is not None
    assert space.space_id.startswith("local:")


# --- MemoryAccess 어댑터 ------------------------------------------------------


@pytest.fixture
def access():
    """조립된 토큰을 물린 어댑터 — 실제 경로로 검증한다."""
    store = SqliteMemoryStore()
    service = MemoryService(store=store)
    token = issue_token(manifest(**LOCAL))
    space_id = token.resolve("longterm").space_id  # type: ignore[union-attr]
    registry = IndexRegistry()
    registry.indexes[space_id] = SpaceIndex(space=space_id)
    try:
        yield (
            ServiceMemoryAccess(
                service=service, token=token, recall=Recall(indexes=registry.indexes)
            ),
            space_id,
            registry,
        )
    finally:
        store.close()


def entry(space: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        space=space,
        kind=MemoryKind.FACT,
        content=content,
        source=MemorySource(agent="test-agent"),
    )


async def test_append_then_search_round_trips(access):
    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")

    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))
    found = await adapter.search("sidecar")

    assert [scored.entry.entry_id for scored in found] == [item.entry_id]


async def test_searching_an_undeclared_space_is_denied(access):
    """인덱스는 서비스를 거치지 않으므로 어댑터가 스스로 경계를 지켜야 한다."""
    adapter, _space_id, _registry = access

    with pytest.raises(MalkuthError) as exc_info:
        await adapter.search("무엇이든", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_search_denies_before_touching_the_store(access):
    """서비스 호출이 우연히 막아주는 것에 기대면, 저장소를 안 거치는 경로가
    생기는 순간 구멍이 된다 — 어댑터가 먼저 거부해야 한다."""
    adapter, _space_id, _registry = access
    adapter.service = None  # type: ignore[assignment]

    with pytest.raises(MalkuthError) as exc_info:
        await adapter.search("무엇이든", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_appending_to_an_undeclared_space_is_denied(access):
    adapter, space_id, _registry = access

    with pytest.raises(MalkuthError) as exc_info:
        await adapter.append("secret", entry=entry(space_id, "내용"))

    assert exc_info.value.code == ErrorCode.MEM_001
