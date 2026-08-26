"""Node execution over the Agent Control API.

오케스트레이터의 ``NodeRuntime`` 계약을 Control API 호출로 구현한다.
그래프는 이 계약 뒤의 컨테이너 사정을 알지 못한다 — 노드 하나가 어느
컨테이너의 어느 포트에 있는지는 runtime 만 안다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from malkuth.core.agent import TaskRequest, TaskResult
    from malkuth.orchestrator.topology import NodeSpec
    from malkuth.runtime.control import ControlClient

log = structlog.get_logger(__name__)


def agent_of(ref: str) -> str:
    """``agents/{name}@{version}`` 에서 이름만 뽑는다."""
    _, _, remainder = ref.partition("/")
    name, _, _ = remainder.partition("@")
    return name


@dataclass
class ControlNodeRuntime:
    """Routes graph nodes to their agents' Control APIs.

    그래프 노드를 담당 에이전트의 Control API 로 라우팅한다.

    Attributes:
        clients: Agent name to its Control API client.
    """

    clients: Mapping[str, ControlClient]
    invoked: list[str] = field(default_factory=list)
    """호출된 노드 순서 — 라우팅 추적과 trace 출력에 쓴다."""

    async def invoke(self, node: NodeSpec, task: TaskRequest) -> TaskResult:
        """Execute one graph node.

        노드 하나를 실행합니다.

        Args:
            node: The graph node being executed.
            task: The task to hand to its agent.

        Returns:
            The agent's result.

        Raises:
            MalkuthError: GRAPH/``GRAPH_002`` if the node declares no agent or
                the agent has no running container — 노드가 가리키는 대상이
                없으면 그래프가 진행할 수 없습니다.
        """
        if node.agent is None:
            # agent 가 없는 노드는 subgraph 다. 이 런타임은 에이전트 호출만
            # 담당하므로, 조용히 건너뛰는 대신 명확히 실패한다 —
            # 건너뛰면 그래프가 실행된 것처럼 보이면서 아무 일도 하지 않는다
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_002,
                message=f"node is not bound to an agent: {node.id}",
                details={"node_id": node.id, "graph_ref": node.graph},
            )

        name = agent_of(node.agent)
        client = self.clients.get(name)
        if client is None:
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_002,
                message=f"no running container for agent: {name}",
                agent=name,
                details={"node_id": node.id, "module_ref": node.agent},
            )

        self.invoked.append(node.id)
        log.info(
            "graph node invoking agent",
            agent=name,
            node_id=node.id,
            run_id=task.run_id,
            task_id=task.task_id,
        )
        return await client.invoke(task)


__all__ = ["ControlNodeRuntime", "agent_of"]
