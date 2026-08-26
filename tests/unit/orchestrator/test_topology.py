"""Unit tests for graph topology schema and deploy-time validation.

검증 규칙 하나당 최소 한 개의 실패 케이스를 둔다 — 배포를 막아야 할 그래프가
통과하는 일이 없도록.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from malkuth.core.errors import ErrorCategory, MalkuthError
from malkuth.orchestrator.topology import (
    GraphMode,
    GraphTopology,
    IdlePolicy,
    resolve_import_ref,
    validate_topology,
)
from tests.fixtures.topologies import (
    condition_ref,
    make_mission,
    make_service,
    mission_dict,
    service_dict,
)


def assert_graph_001(exc_info: pytest.ExceptionInfo[MalkuthError]) -> None:
    """토폴로지 검증 실패는 전부 GRAPH_001 로 보고된다."""
    assert exc_info.value.code == "GRAPH_001"
    assert exc_info.value.category is ErrorCategory.GRAPH


def test_valid_mission_topology_passes():
    validate_topology(make_mission())


def test_valid_service_topology_passes():
    validate_topology(make_service())


def test_topology_is_frozen():
    topology = make_mission()

    with pytest.raises(ValidationError):
        topology.metadata.name = "other"  # type: ignore[misc]


# --- 스키마 수준 규칙 -------------------------------------------------------


@pytest.mark.parametrize("reserved", ["START", "END"])
def test_reserved_node_id_is_rejected(reserved):
    with pytest.raises(ValidationError, match="reserved"):
        GraphTopology.model_validate(
            mission_dict(nodes=[{"id": reserved, "agent": "agents/a@0.1.0"}])
        )


def test_duplicate_node_id_is_rejected():
    with pytest.raises(ValidationError, match="duplicate node id"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[
                    {"id": "planner", "agent": "agents/a@0.1.0"},
                    {"id": "planner", "agent": "agents/b@0.1.0"},
                ]
            )
        )


def test_empty_node_list_is_rejected():
    with pytest.raises(ValidationError, match="at least one node"):
        GraphTopology.model_validate(mission_dict(nodes=[]))


def test_node_requires_exactly_one_target():
    with pytest.raises(ValidationError, match="exactly one of 'agent' or 'graph'"):
        GraphTopology.model_validate(mission_dict(nodes=[{"id": "n"}]))


def test_node_rejects_both_agent_and_graph():
    with pytest.raises(ValidationError, match="exactly one of 'agent' or 'graph'"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[{"id": "n", "agent": "agents/a@0.1.0", "graph": "graphs/sub@1.0.0"}]
            )
        )


def test_self_connection_is_rejected():
    with pytest.raises(ValidationError, match="caller and callee must differ"):
        GraphTopology.model_validate(
            mission_dict(connections=[{"caller": "planner", "callee": "planner"}])
        )


def test_service_mode_requires_idle_policy():
    """service.idle 미선언 시 검증 실패 — 04 Graph Rules 3."""
    raw = service_dict()
    del raw["spec"]["service"]

    with pytest.raises(ValidationError, match="requires 'service.idle'"):
        GraphTopology.model_validate(raw)


def test_mission_mode_rejects_service_settings():
    with pytest.raises(ValidationError, match="only valid in service mode"):
        GraphTopology.model_validate(
            mission_dict(service={"idle": {"min_delay_s": 1, "max_delay_s": 2}})
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("min_delay_s", 0), ("max_delay_s", 1), ("multiplier", 1.0)],
)
def test_invalid_idle_policy_is_rejected(field, value):
    kwargs = {"min_delay_s": 30.0, "max_delay_s": 600.0, "multiplier": 2.0, field: value}

    with pytest.raises(ValidationError):
        IdlePolicy(**kwargs)


def test_idle_backoff_progresses_and_clamps():
    policy = IdlePolicy(min_delay_s=30, max_delay_s=240, multiplier=2.0)

    assert [policy.delay_for(s) for s in range(5)] == [30, 60, 120, 240, 240]


def test_idle_delay_rejects_negative_streak():
    with pytest.raises(ValueError, match="streak must be >= 0"):
        IdlePolicy().delay_for(-1)


def test_max_failure_streak_must_be_positive():
    with pytest.raises(ValidationError, match="max_failure_streak must be >= 1"):
        GraphTopology.model_validate(
            service_dict(
                service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 0}
            )
        )


# --- 검증기 규칙 ------------------------------------------------------------


def test_dangling_edge_target_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "ghost"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "dangling edge to" in exc_info.value.message


def test_dangling_edge_source_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "ghost", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "dangling edge from" in exc_info.value.message


def test_edge_out_of_end_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
            {"from": "END", "to": "planner"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "END must not have outgoing edges" in exc_info.value.message


def test_edge_into_start_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "START"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "START must not have incoming edges" in exc_info.value.message


def test_unreachable_node_is_rejected():
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0"},
            {"id": "orphan", "agent": "agents/orphan@0.1.0"},
        ],
        edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "unreachable from START" in exc_info.value.message


def test_mission_without_end_reachability_is_rejected():
    """mission 은 END 도달 필수 — 04 Graph Rules 3."""
    topology = make_mission(
        nodes=[{"id": "planner", "agent": "agents/planner@0.1.0"}],
        edges=[{"from": "START", "to": "planner"}],
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "must be able to reach END" in exc_info.value.message


def test_mission_cycle_without_max_iterations_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "planner"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "requires 'max_iterations'" in exc_info.value.message


def test_mission_cycle_with_max_iterations_passes():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "planner", "max_iterations": 3},
            {"from": "researcher", "to": "END"},
        ]
    )

    validate_topology(topology)


def test_service_graph_must_reach_end():
    """service 도 END 로 끝나야 한다 — 반복은 그래프가 아니라 runner 가 소유한다.

    그래프가 스스로 순환하면 한 번의 실행이 끝나지 않아 ServiceRunner 의
    iteration 경계가 성립하지 않는다.
    """
    topology = make_service(
        edges=[
            {"from": "START", "to": "watcher"},
            {"from": "watcher", "to": "notifier"},
            {"from": "notifier", "to": "watcher"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "service graph must be able to reach END" in exc_info.value.message


def test_service_cycle_requires_max_iterations():
    """그래프 안에 남긴 순환은 mission 과 같은 상한 규칙을 받는다."""
    topology = make_service(
        edges=[
            {"from": "START", "to": "watcher"},
            {"from": "watcher", "to": "notifier"},
            {"from": "notifier", "to": "watcher", "condition": condition_ref()},
            {"from": "notifier", "to": "END", "condition": condition_ref()},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "max_iterations" in exc_info.value.message


def test_connection_caller_must_be_a_node():
    topology = make_mission(connections=[{"caller": "ghost", "callee": "planner"}])

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "connection caller is not a graph node" in exc_info.value.message


def test_connection_callee_must_be_a_node():
    topology = make_mission(connections=[{"caller": "planner", "callee": "ghost"}])

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "connection callee is not a graph node" in exc_info.value.message


def test_valid_connection_passes():
    validate_topology(make_mission(connections=[{"caller": "researcher", "callee": "planner"}]))


def test_unimportable_condition_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher", "condition": "no.such.module:fn"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "cannot import module" in exc_info.value.message


def test_missing_condition_attribute_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:no_such_fn",
            },
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert "attribute not found" in exc_info.value.message


def test_input_map_unknown_state_field_is_rejected():
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"q": "state.nonexistent"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology, state_fields=frozenset({"query", "plan"}))

    assert_graph_001(exc_info)
    assert "unknown state field" in exc_info.value.message


def test_input_map_known_state_field_passes():
    validate_topology(make_mission(), state_fields=frozenset({"query", "plan"}))


def test_input_map_literal_source_is_not_checked():
    """state. 접두사가 없으면 리터럴이므로 schema 대조 대상이 아니다."""
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "input_map": {"mode": "fast"}},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    validate_topology(topology, state_fields=frozenset({"query"}))


# --- ref 해석 ---------------------------------------------------------------


def test_resolve_import_ref_returns_attribute():
    fn = resolve_import_ref("malkuth.graphs.conditions:needs_research")

    assert fn({"needs_research": True}) is True


def test_resolve_import_ref_rejects_missing_separator():
    with pytest.raises(MalkuthError) as exc_info:
        resolve_import_ref("malkuth.graphs.conditions.needs_research")

    assert "invalid import ref" in exc_info.value.message


# --- 노드 조회 --------------------------------------------------------------


def test_node_lookup():
    topology = make_mission()

    assert topology.node("planner").ref == "agents/planner@0.1.0"
    assert topology.node_ids == frozenset({"planner", "researcher"})
    assert topology.mode is GraphMode.MISSION


def test_node_lookup_missing_raises_key_error():
    with pytest.raises(KeyError):
        make_mission().node("ghost")


def test_subgraph_node_ref():
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0"},
            {"id": "review", "graph": "graphs/sub-review@1.0.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "review"},
            {"from": "review", "to": "END"},
        ],
    )

    review = topology.node("review")
    assert review.is_subgraph is True
    assert review.ref == "graphs/sub-review@1.0.0"


def test_import_ref_with_empty_module_is_wrapped_as_graph_001():
    """빈 모듈명은 ValueError 를 내지만, 계약상 GRAPH_001 로 보고돼야 한다."""
    with pytest.raises(MalkuthError) as exc_info:
        resolve_import_ref(":attr")

    assert_graph_001(exc_info)


@pytest.mark.parametrize("value", [0, -1, -5])
def test_non_positive_max_iterations_is_rejected(value):
    """음수는 truthy 라 상한 검사를 조용히 통과시킨다 — 스키마에서 막는다."""
    with pytest.raises(ValidationError, match="max_iterations must be >= 1"):
        GraphTopology.model_validate(
            mission_dict(
                edges=[
                    {"from": "START", "to": "planner"},
                    {"from": "planner", "to": "researcher"},
                    {"from": "researcher", "to": "planner", "max_iterations": value},
                    {"from": "researcher", "to": "END"},
                ]
            )
        )


def test_negative_retry_is_rejected():
    with pytest.raises(ValidationError, match="retry must be >= 0"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[
                    {"id": "planner", "agent": "agents/planner@0.1.0", "retry": -1},
                    {"id": "researcher", "agent": "agents/researcher@0.1.0"},
                ]
            )
        )


@pytest.mark.parametrize("value", [0, -1.5])
def test_non_positive_node_timeout_is_rejected(value):
    with pytest.raises(ValidationError, match="timeout_s must be > 0"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[
                    {"id": "planner", "agent": "agents/planner@0.1.0", "timeout_s": value},
                    {"id": "researcher", "agent": "agents/researcher@0.1.0"},
                ]
            )
        )


def test_max_iterations_outside_the_cycle_does_not_satisfy_the_rule():
    """무관한 edge 에 붙은 상한은 실제 순환을 제한하지 못한다."""
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner", "max_iterations": 3},  # cycle 밖
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "planner"},  # 실제 순환 — 상한 없음
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "max_iterations" in exc_info.value.message


def test_max_iterations_on_the_cycle_edge_satisfies_the_rule():
    validate_topology(
        make_mission(
            edges=[
                {"from": "START", "to": "planner"},
                {"from": "planner", "to": "researcher"},
                {"from": "researcher", "to": "planner", "max_iterations": 3},
                {"from": "researcher", "to": "END"},
            ]
        )
    )


def test_self_loop_requires_max_iterations():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(MalkuthError):
        validate_topology(topology)


def test_input_map_preserves_non_string_literals():
    """YAML 의 숫자/불리언 리터럴이 문자열로 강제되지 않아야 한다."""
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"depth": 2, "verbose": True, "q": "state.query"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    input_map = topology.node("planner").input_map
    assert input_map["depth"] == 2
    assert input_map["verbose"] is True


def test_input_map_validation_ignores_non_string_literals():
    """리터럴은 state 참조가 아니므로 schema 대조 대상이 아니다."""
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "input_map": {"depth": 2}},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    validate_topology(topology, state_fields=frozenset({"query"}))
