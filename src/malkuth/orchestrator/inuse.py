"""Which declarations a running run depends on.

authoring 은 배포 중인 선언을 지우거나 덮어쓰지 못하게 `in_use` 를 묻는다 (#242).
프로덕션 `Author` 에 그 술어가 비어 있었다 — 실행 중 run 의 그래프와 에이전트가
발밑에서 바뀔 수 있었다. 여기서는 **run 저장소**를 본다. 배포(#243)는 또 다른
원천이고, 그 둘을 OR 로 묶는 것이 최종 형태다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from malkuth.orchestrator.run import RunStatus

if TYPE_CHECKING:
    from malkuth.authoring import InUse
    from malkuth.catalog import Catalog
    from malkuth.orchestrator.runstore import RunStore

ACTIVE = frozenset({str(RunStatus.RUNNING), str(RunStatus.DRAINING)})
"""드레인 중도 아직 그 그래프로 돈다 — 끝날 때까지 선언은 고정이다."""


def _agent_of(ref: str) -> str:
    return ref.split("/", 1)[1].split("@", 1)[0]


def run_backed(store: RunStore, catalog: Catalog) -> InUse:
    """Build an ``in_use`` predicate from live runs.

    실행 중 run 이 참조하는 그래프와, 그 그래프의 노드가 쓰는 에이전트를 사용 중으로 본다.
    """

    def in_use(kind: str, name: str) -> bool:
        active_graphs = {r.graph for r in store.list() if r.status in ACTIVE}
        if kind == "graph":
            return name in active_graphs
        for graph_name in active_graphs:
            try:
                graph = catalog.graph(graph_name)
            except Exception:  # noqa: BLE001 — 깨진 그래프는 카탈로그가 따로 보고한다
                continue
            if any(n.agent is not None and _agent_of(n.agent) == name for n in graph.spec.nodes):
                return True
        return False

    return in_use


__all__ = ["ACTIVE", "run_backed"]
