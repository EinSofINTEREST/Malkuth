"""Graph topology schema and deploy-time validation.

그래프 토폴로지 스키마와 배포 시점 검증. 그래프는 에이전트를 잇고 분리하는
배선 모듈이며, 연결 변경은 이 YAML 수정만으로 완료되어야 한다.

검증 실패는 전부 ``GRAPH_001`` 로 배포를 중단시킨다 — 잘못된 토폴로지로는
컨테이너를 기동하지 않는다.
"""

from __future__ import annotations

import importlib
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import AgentName, MemorySpec, SemVer

if TYPE_CHECKING:
    from malkuth.modules.registry import ModuleRegistry

START = "START"
END = "END"
RESERVED_NODE_IDS = frozenset({START, END})

DEFAULT_MAX_FAILURE_STREAK = 5
DEFAULT_IDLE_MIN_DELAY_S = 30.0
DEFAULT_IDLE_MAX_DELAY_S = 600.0

_IMPORT_REF_SEPARATOR = ":"


class GraphMode(StrEnum):
    """그래프 실행 모드.

    mission 은 목표 달성 후 종료하고, service 는 무한히 반복한다.
    """

    MISSION = "mission"
    SERVICE = "service"


def _topology_error(message: str, **details: Any) -> MalkuthError:
    """토폴로지 검증 실패를 ``GRAPH_001`` 로 만든다."""
    return MalkuthError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_001,
        message=message,
        details=details,
    )


class NodeSpec(BaseModel):
    """A graph node bound to an agent or a subgraph.

    그래프 노드. 에이전트 또는 서브그래프 중 정확히 하나를 참조한다.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    agent: str | None = None
    graph: str | None = None
    input_map: dict[str, str] = Field(default_factory=dict)
    output_map: dict[str, str] = Field(default_factory=dict)
    retry: int = 0
    timeout_s: float | None = None

    @field_validator("id")
    @classmethod
    def _reject_reserved_ids(cls, value: str) -> str:
        """START/END 는 예약어이므로 노드 id 로 쓸 수 없다."""
        if value in RESERVED_NODE_IDS:
            raise ValueError(f"node id '{value}' is reserved")
        if not value:
            raise ValueError("node id must not be empty")
        return value

    @model_validator(mode="after")
    def _exactly_one_target(self) -> NodeSpec:
        """노드는 에이전트 또는 서브그래프 중 하나만 참조한다."""
        if (self.agent is None) == (self.graph is None):
            raise ValueError("node requires exactly one of 'agent' or 'graph'")
        return self

    @property
    def is_subgraph(self) -> bool:
        """서브그래프 노드인지."""
        return self.graph is not None

    @property
    def ref(self) -> str:
        """참조 문자열 — 에이전트 또는 그래프 ref."""
        return self.agent if self.agent is not None else str(self.graph)


class EdgeSpec(BaseModel):
    """A directed edge, optionally conditional.

    방향 간선. ``condition`` 이 있으면 조건부 라우팅이다.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    condition: str | None = None
    max_iterations: int | None = None


class ConnectionSpec(BaseModel):
    """An allowlisted A2A peer call.

    A2A 호출 allowlist 항목. 방향은 선언의 문제이며 peer 간 우열은 없다 —
    역방향이 필요하면 별도로 선언한다.
    """

    model_config = ConfigDict(frozen=True)

    caller: str
    callee: str

    @model_validator(mode="after")
    def _reject_self_call(self) -> ConnectionSpec:
        """자기 자신에 대한 A2A 선언은 의미가 없다."""
        if self.caller == self.callee:
            raise ValueError("connection caller and callee must differ")
        return self


class IdlePolicy(BaseModel):
    """Exponential backoff while a service graph has no work.

    Service 그래프의 idle 정책. busy-loop 로 모델 호출을 낭비하지 않도록
    작업이 없으면 지수 백오프한다.
    """

    model_config = ConfigDict(frozen=True)

    min_delay_s: float = DEFAULT_IDLE_MIN_DELAY_S
    max_delay_s: float = DEFAULT_IDLE_MAX_DELAY_S
    multiplier: float = 2.0

    @model_validator(mode="after")
    def _check_bounds(self) -> IdlePolicy:
        """상한이 하한보다 작으면 백오프가 성립하지 않는다."""
        if self.min_delay_s <= 0:
            raise ValueError("idle min_delay_s must be positive")
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("idle max_delay_s must be >= min_delay_s")
        if self.multiplier <= 1:
            raise ValueError("idle multiplier must be > 1")
        return self

    def delay_for(self, streak: int) -> float:
        """Compute the idle delay after ``streak`` consecutive idle iterations.

        연속 idle 횟수에 대한 대기 시간을 계산합니다. 0 이면 첫 idle 이므로
        ``min_delay_s`` 를 반환하고, 상한에서 고정됩니다.
        """
        if streak < 0:
            raise ValueError("idle streak must be >= 0")
        delay = self.min_delay_s * (self.multiplier**streak)
        return min(delay, self.max_delay_s)


class ServiceSpec(BaseModel):
    """Service-mode settings.

    상주형 실행 설정. idle 정책은 필수이며, 연속 실패 임계를 넘으면 정지한다.
    """

    model_config = ConfigDict(frozen=True)

    idle: IdlePolicy
    max_failure_streak: int = DEFAULT_MAX_FAILURE_STREAK

    @field_validator("max_failure_streak")
    @classmethod
    def _positive_streak(cls, value: int) -> int:
        """0 이면 첫 실패에 정지하므로 crash loop 방지 의미가 없다."""
        if value < 1:
            raise ValueError("max_failure_streak must be >= 1")
        return value


class StateSpec(BaseModel):
    """Graph state schema binding.

    그래프 state 스키마 바인딩. ``schema`` 는 pydantic 모델 import ref 다.
    """

    model_config = ConfigDict(frozen=True)

    schema_ref: str = Field(alias="schema")
    checkpointer: str = "default"


class GraphMetadata(BaseModel):
    """Graph identity.

    그래프 식별 정보. 토폴로지 변경 시 version 을 bump 한다.
    """

    model_config = ConfigDict(frozen=True)

    name: AgentName
    version: SemVer
    description: str | None = None


class GraphSpec(BaseModel):
    """The graph wiring body.

    그래프 배선 본문. 노드 추가/제거와 edge 연결/분리가 전부 여기서 이루어진다.
    """

    model_config = ConfigDict(frozen=True)

    mode: GraphMode = GraphMode.MISSION
    goal: str
    state: StateSpec
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    connections: tuple[ConnectionSpec, ...] = ()
    service: ServiceSpec | None = None
    memory: MemorySpec = Field(default_factory=MemorySpec)

    @field_validator("nodes")
    @classmethod
    def _unique_node_ids(cls, value: tuple[NodeSpec, ...]) -> tuple[NodeSpec, ...]:
        """노드 id 중복 금지 — 라우팅이 모호해진다."""
        if not value:
            raise ValueError("graph must declare at least one node")
        ids = [n.id for n in value]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate node id: {sorted(duplicates)}")
        return value

    @model_validator(mode="after")
    def _check_mode_requirements(self) -> GraphSpec:
        """모드별 필수 선언을 확인한다."""
        if self.mode is GraphMode.SERVICE and self.service is None:
            raise ValueError("service mode requires 'service.idle' policy")
        if self.mode is GraphMode.MISSION and self.service is not None:
            raise ValueError("'service' settings are only valid in service mode")
        return self


class GraphTopology(BaseModel):
    """A deployable graph module.

    배포 가능한 그래프 모듈. 배포 시 :func:`validate_topology` 로 검증된 뒤에만
    컨테이너가 기동된다.
    """

    model_config = ConfigDict(frozen=True)

    api_version: Literal["malkuth/v1"] = Field(alias="apiVersion")
    kind: Literal["Graph"]
    metadata: GraphMetadata
    spec: GraphSpec

    @property
    def name(self) -> str:
        """그래프 이름."""
        return self.metadata.name

    @property
    def mode(self) -> GraphMode:
        """실행 모드."""
        return self.spec.mode

    @property
    def node_ids(self) -> frozenset[str]:
        """선언된 노드 id 집합."""
        return frozenset(n.id for n in self.spec.nodes)

    def node(self, node_id: str) -> NodeSpec:
        """Look up a node by id.

        id 로 노드를 찾습니다.

        Raises:
            KeyError: If no node with that id is declared.
        """
        for candidate in self.spec.nodes:
            if candidate.id == node_id:
                return candidate
        raise KeyError(node_id)


ImportRef = Annotated[str, Field(pattern=r"^[\w.]+:[\w.]+$")]
"""Importable reference — ``module.path:attribute``."""


def resolve_import_ref(ref: str) -> Any:
    """Import an object from a ``module:attribute`` reference.

    ``module:attribute`` 형식의 참조를 import 합니다.

    Args:
        ref: Importable reference such as ``malkuth.graphs.conditions:needs_research``.

    Returns:
        The imported attribute.

    Raises:
        MalkuthError: GRAPH/``GRAPH_001`` if the reference cannot be imported.
    """
    if _IMPORT_REF_SEPARATOR not in ref:
        raise _topology_error(f"invalid import ref: {ref}", ref=ref)

    module_path, _, attribute = ref.partition(_IMPORT_REF_SEPARATOR)
    try:
        module = importlib.import_module(module_path)
    except ImportError as err:
        raise _topology_error(f"cannot import module for ref: {ref}", ref=ref) from err

    try:
        return getattr(module, attribute)
    except AttributeError as err:
        raise _topology_error(f"attribute not found for ref: {ref}", ref=ref) from err


def _check_edge_endpoints(topology: GraphTopology) -> None:
    """dangling edge 검출 — from/to 가 노드 또는 START/END 여야 한다."""
    valid = topology.node_ids | RESERVED_NODE_IDS
    for edge in topology.spec.edges:
        for role, endpoint in (("from", edge.source), ("to", edge.target)):
            if endpoint not in valid:
                raise _topology_error(
                    f"dangling edge {role}: {endpoint}",
                    graph=topology.name,
                    edge=f"{edge.source}->{edge.target}",
                )
        if edge.source == END:
            raise _topology_error(
                "END must not have outgoing edges",
                graph=topology.name,
                edge=f"{edge.source}->{edge.target}",
            )
        if edge.target == START:
            raise _topology_error(
                "START must not have incoming edges",
                graph=topology.name,
                edge=f"{edge.source}->{edge.target}",
            )


def _adjacency(topology: GraphTopology) -> dict[str, set[str]]:
    """노드 id → 도달 가능한 다음 노드 집합."""
    graph: dict[str, set[str]] = {node_id: set() for node_id in topology.node_ids}
    graph[START] = set()
    graph[END] = set()
    for edge in topology.spec.edges:
        graph[edge.source].add(edge.target)
    return graph


def _reachable_from_start(topology: GraphTopology) -> set[str]:
    """START 에서 도달 가능한 노드 집합 (BFS)."""
    graph = _adjacency(topology)
    seen: set[str] = set()
    frontier = [START]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(graph.get(current, set()) - seen)
    return seen


def _check_reachability(topology: GraphTopology) -> None:
    """START 에서 모든 노드에 도달 가능해야 한다."""
    reachable = _reachable_from_start(topology)
    unreachable = topology.node_ids - reachable
    if unreachable:
        raise _topology_error(
            f"nodes unreachable from START: {sorted(unreachable)}",
            graph=topology.name,
        )


def _has_cycle(topology: GraphTopology) -> bool:
    """방향 순환 존재 여부 (self-loop 포함)."""
    graph = _adjacency(topology)
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        found = any(walk(nxt) for nxt in graph.get(node, set()))
        visiting.discard(node)
        visited.add(node)
        return found

    return walk(START)


def _check_mode_topology(topology: GraphTopology) -> None:
    """모드별 토폴로지 규칙 — mission 은 END 도달, cycle 은 max_iterations 필수."""
    spec = topology.spec
    if spec.mode is GraphMode.MISSION:
        if END not in _reachable_from_start(topology):
            raise _topology_error("mission graph must be able to reach END", graph=topology.name)
        if _has_cycle(topology) and not any(e.max_iterations for e in spec.edges):
            raise _topology_error(
                "mission graph with a cycle requires 'max_iterations' on a cycle edge",
                graph=topology.name,
            )


def _check_connections(topology: GraphTopology) -> None:
    """A2A allowlist 의 caller/callee 가 모두 그래프 노드여야 한다."""
    for connection in topology.spec.connections:
        for role, node_id in (("caller", connection.caller), ("callee", connection.callee)):
            if node_id not in topology.node_ids:
                raise _topology_error(
                    f"connection {role} is not a graph node: {node_id}",
                    graph=topology.name,
                )


def _check_conditions(topology: GraphTopology) -> None:
    """conditional edge 의 조건 함수가 import 가능해야 한다."""
    for edge in topology.spec.edges:
        if edge.condition is not None:
            resolve_import_ref(edge.condition)


def _check_input_maps(topology: GraphTopology, state_fields: frozenset[str]) -> None:
    """input_map 이 참조하는 state 키가 schema 에 존재해야 한다."""
    for node in topology.spec.nodes:
        for target_key, source in node.input_map.items():
            if not source.startswith("state."):
                continue
            field = source.removeprefix("state.").split(".", 1)[0]
            if field not in state_fields:
                raise _topology_error(
                    f"input_map references unknown state field: {source}",
                    graph=topology.name,
                    node_id=node.id,
                    details_key=target_key,
                )


def _check_refs(topology: GraphTopology, registry: ModuleRegistry) -> None:
    """노드가 참조하는 에이전트/서브그래프 ref 가 해석 가능해야 한다."""
    for node in topology.spec.nodes:
        try:
            registry.resolve(node.ref)
        except MalkuthError as err:
            raise _topology_error(
                f"cannot resolve node ref: {node.ref}",
                graph=topology.name,
                node_id=node.id,
                module_ref=node.ref,
            ) from err


class SubgraphLoader(Protocol):
    """Resolves a graph ref to its topology.

    그래프 ref 를 토폴로지로 해석하는 계약 — 서브그래프 순환 검사에 사용한다.
    """

    def __call__(self, ref: str) -> GraphTopology:
        """참조를 해석해 토폴로지를 반환한다."""
        ...


def _check_subgraph_cycles(
    topology: GraphTopology,
    load: SubgraphLoader,
    seen: tuple[str, ...],
) -> None:
    """서브그래프 순환 참조를 차단한다."""
    for node in topology.spec.nodes:
        if not node.is_subgraph:
            continue
        ref = node.ref
        if ref in seen:
            raise _topology_error(
                f"subgraph cycle detected: {' -> '.join([*seen, ref])}",
                graph=topology.name,
                node_id=node.id,
            )
        child = load(ref)
        _check_subgraph_cycles(child, load, (*seen, ref))


def validate_topology(
    topology: GraphTopology,
    *,
    registry: ModuleRegistry | None = None,
    state_fields: frozenset[str] | None = None,
    load_subgraph: SubgraphLoader | None = None,
) -> None:
    """Validate a graph topology at deploy time.

    배포 시점에 그래프 토폴로지를 검증합니다. 하나라도 실패하면 배포를 중단해야
    하므로 첫 위반에서 즉시 ``GRAPH_001`` 을 발생시킵니다.

    Args:
        topology: The topology to validate.
        registry: Module registry used to resolve node refs; skipped when omitted.
        state_fields: Field names of the graph state schema; skipped when omitted.
        load_subgraph: Callable resolving a graph ref to a topology, for cycle checks.

    Raises:
        MalkuthError: GRAPH/``GRAPH_001`` on the first rule violation.
    """
    _check_edge_endpoints(topology)
    _check_reachability(topology)
    _check_mode_topology(topology)
    _check_connections(topology)
    _check_conditions(topology)

    if state_fields is not None:
        _check_input_maps(topology, state_fields)
    if registry is not None:
        _check_refs(topology, registry)
    if load_subgraph is not None:
        _check_subgraph_cycles(topology, load_subgraph, (topology.name,))
