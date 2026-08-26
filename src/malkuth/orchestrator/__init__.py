"""LangGraph orchestration — topology, state, builder, checkpoint.

그래프 오케스트레이션. LangGraph API 를 직접 만지는 유일한 레이어이며,
Docker/프로토콜에는 runtime 추상 계약을 통해서만 접근한다.
"""

from malkuth.orchestrator.builder import GraphBuilder, NodeRuntime, build_graph
from malkuth.orchestrator.checkpoint import (
    DEFAULT_CHECKPOINTER,
    CheckpointerKind,
    build_checkpointer,
    guarded_restore,
    guarded_save,
)
from malkuth.orchestrator.state import (
    extract_input,
    merge_output,
    resolve_state_schema,
    state_fields,
    validate_state,
)
from malkuth.orchestrator.topology import (
    END,
    START,
    ConnectionSpec,
    EdgeSpec,
    GraphMetadata,
    GraphMode,
    GraphSpec,
    GraphTopology,
    IdlePolicy,
    NodeSpec,
    ServiceSpec,
    StateSpec,
    SubgraphLoader,
    resolve_import_ref,
    validate_topology,
)

__all__ = [
    "DEFAULT_CHECKPOINTER",
    "END",
    "START",
    "CheckpointerKind",
    "ConnectionSpec",
    "EdgeSpec",
    "GraphBuilder",
    "GraphMetadata",
    "GraphMode",
    "GraphSpec",
    "GraphTopology",
    "IdlePolicy",
    "NodeRuntime",
    "NodeSpec",
    "ServiceSpec",
    "StateSpec",
    "SubgraphLoader",
    "build_checkpointer",
    "build_graph",
    "extract_input",
    "guarded_restore",
    "guarded_save",
    "merge_output",
    "resolve_import_ref",
    "resolve_state_schema",
    "state_fields",
    "validate_state",
    "validate_topology",
]
