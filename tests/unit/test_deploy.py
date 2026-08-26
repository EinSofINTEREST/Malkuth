"""Unit tests for deploy-time contract validation.

**하나라도 실패하면 컨테이너를 기동하지 않는다.** 8항목 각각의 실패 케이스와,
여러 항목이 동시에 실패할 때 전부 보고되는지를 검증한다 — 첫 실패에서 멈추면
운영자가 고칠 때마다 처음부터 다시 돌려야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from malkuth.core.errors import MalkuthError
from malkuth.deploy import validate_deployment
from malkuth.orchestrator.topology import GraphTopology
from tests.fixtures.builders import make_manifest
from tests.fixtures.topologies import mission_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
REFS = frozenset({"promptsets/test@0.1.0"})


def make_topology(agents, *, connections=None, edges=None):
    """지정한 에이전트들을 노드로 갖는 mission 토폴로지."""
    nodes = [{"id": name, "agent": f"agents/{name}@0.1.0"} for name in agents]
    if edges is None:
        chain: list[dict[str, str]] = [{"from": "START", "to": agents[0]}]
        chain.extend({"from": a, "to": b} for a, b in zip(agents, agents[1:], strict=False))
        chain.append({"from": agents[-1], "to": "END"})
    else:
        chain = [{"from": source, "to": target} for source, target in edges]

    overrides: dict = {"nodes": nodes, "edges": chain}
    if connections is not None:
        overrides["connections"] = [
            {"caller": caller, "callee": callee} for caller, callee in connections
        ]
    return GraphTopology.model_validate(mission_dict(**overrides))


def agent(name: str, **spec_extra) -> object:
    """레퍼런스 최소 manifest."""
    spec = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": "promptsets/test@0.1.0"},
        **spec_extra,
    }
    return make_manifest(metadata={"name": name, "version": "0.1.0"}, spec=spec)


def group(name: str, **spec_extra):
    """그룹 manifest."""
    from malkuth.core.manifest import GroupManifest

    return GroupManifest.model_validate(
        {
            "apiVersion": "malkuth/v1",
            "kind": "Group",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {"quotas": {"cpu": "8.0", "memory": "16Gi", "max_agents": 10}, **spec_extra},
        }
    )


def run(topologies=None, **overrides):
    """검증 실행 — 기본은 전부 통과하는 구성."""
    manifests = overrides.pop("manifests", None) or {"planner": agent("planner")}
    options = {
        "manifests": manifests,
        "resolvable_refs": REFS,
        **overrides,
    }
    return validate_deployment(topologies or [], **options)


# --- 정상 경로 ----------------------------------------------------------------


def test_valid_deployment_passes():
    report = run([make_topology(["planner"])])

    assert report.ok
    assert report.findings == ()


def test_empty_deployment_passes():
    assert run().ok


# --- 1. agent ref -------------------------------------------------------------


def test_unknown_agent_ref_is_reported():
    """dangling agent ref 로 기동하면 노드 실행 시점에야 실패한다."""
    report = run([make_topology(["absent"])])

    assert not report.ok
    assert "agent_refs" in report.checks()
    assert "MOD_001" in [str(c) for c in report.codes()]


# --- 2. 모듈 ref --------------------------------------------------------------


def test_unresolvable_promptset_is_reported():
    report = run([make_topology(["planner"])], resolvable_refs=frozenset())

    assert "module_refs" in report.checks()


def test_unresolvable_skillset_is_reported():
    manifests = {"planner": agent("planner", skillsets=[{"ref": "skillsets/absent@1.0.0"}])}

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "module_refs" in report.checks()


def test_unresolvable_memoryset_is_reported():
    manifests = {
        "planner": agent(
            "planner",
            memory={"spaces": [{"ref": "memorysets/absent@1.0.0", "as": "longterm"}]},
        )
    }

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "module_refs" in report.checks()


# --- 3. 그룹 소속 --------------------------------------------------------------


def test_unknown_group_is_reported():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "absent"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
            },
        )
    }

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "groups" in report.checks()
    assert "CFG_002" in [str(c) for c in report.codes()]


def test_declared_group_passes():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "research"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
            },
        )
    }

    report = run(
        [make_topology(["planner"])], manifests=manifests, groups={"research": group("research")}
    )

    assert report.ok


# --- 4. env_allowlist ---------------------------------------------------------


def test_unresolvable_env_key_is_reported():
    """어느 스코프에서도 해석되지 않는 키로 기동하면 컨테이너가 그 값 없이 뜬다."""
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["MYSTERY_KEY"]})}

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "env_allowlist" in report.checks()


def test_local_secret_resolves_the_key():
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["LOCAL_KEY"]})}

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        local_secrets={"planner": frozenset({"LOCAL_KEY"})},
    )

    assert report.ok


def test_group_secret_resolves_the_key():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "research"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
                "runtime": {"env_allowlist": ["SEARCH_API_KEY"]},
            },
        )
    }

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        groups={"research": group("research", secrets=["SEARCH_API_KEY"])},
    )

    assert report.ok


def test_global_secret_resolves_the_key():
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["ANTHROPIC_API_KEY"]})}

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        global_secrets={"ANTHROPIC_API_KEY"},
    )

    assert report.ok


def test_non_member_cannot_use_a_group_secret():
    """그룹 키는 멤버에게만 제공된다 — 비멤버가 allowlist 에 넣어도 해석되지 않는다."""
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["SEARCH_API_KEY"]})}

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        groups={"research": group("research", secrets=["SEARCH_API_KEY"])},
    )

    assert "env_allowlist" in report.checks()


# --- 5. connections -----------------------------------------------------------


def test_connection_to_a_non_node_is_reported():
    topology = make_topology(["planner"], connections=[("planner", "ghost")])

    report = run([topology])

    assert "connections" in report.checks()


def test_connection_callee_without_a2a_is_reported():
    """peer 호출을 받으려면 callee 가 a2a.enabled 여야 한다."""
    manifests = {
        "planner": agent("planner"),
        "researcher": agent("researcher", a2a={"enabled": False}),
    }
    topology = make_topology(["planner", "researcher"], connections=[("planner", "researcher")])

    report = run([topology], manifests=manifests)

    assert "A2A_004" in [str(c) for c in report.codes()]


def test_connection_with_enabled_callee_passes():
    manifests = {
        "planner": agent("planner"),
        "researcher": agent("researcher", a2a={"enabled": True}),
    }
    topology = make_topology(["planner", "researcher"], connections=[("planner", "researcher")])

    report = run([topology], manifests=manifests)

    assert report.ok


# --- 6. mode 규칙 -------------------------------------------------------------


def test_topology_violation_is_reported():
    """토폴로지 검증 실패를 배포 검증이 통과시키면 안 된다."""
    topology = make_topology(["planner"], edges=[("START", "planner")])  # END 미도달

    report = run([topology])

    assert "mode_rules" in report.checks()


# --- 7. A2A 포트 수용량 --------------------------------------------------------


def test_insufficient_port_range_is_reported():
    """범위가 모자라면 마지막 에이전트가 기동에 실패한다."""
    manifests = {name: agent(name, a2a={"enabled": True}) for name in ("planner", "researcher")}

    report = run(
        [make_topology(["planner", "researcher"])],
        manifests=manifests,
        a2a_port_range=(9100, 9100),
    )

    assert "a2a_ports" in report.checks()


def test_sufficient_port_range_passes():
    manifests = {name: agent(name, a2a={"enabled": True}) for name in ("planner", "researcher")}

    report = run(
        [make_topology(["planner", "researcher"])],
        manifests=manifests,
        a2a_port_range=(9100, 9199),
    )

    assert report.ok


def test_agents_without_a2a_do_not_consume_ports():
    manifests = {name: agent(name) for name in ("planner", "researcher")}

    report = run(
        [make_topology(["planner", "researcher"])],
        manifests=manifests,
        a2a_port_range=(9100, 9100),
    )

    assert report.ok


# --- 8. quota -----------------------------------------------------------------


def test_group_quota_excess_is_reported():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "research"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
                "runtime": {"resources": {"cpu": "16.0", "memory": "1Gi"}},
            },
        )
    }

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        groups={"research": group("research")},
    )

    assert "quotas" in report.checks()


def test_host_capacity_excess_is_reported():
    manifests = {
        "planner": agent("planner", runtime={"resources": {"cpu": "4.0", "memory": "1Gi"}})
    }

    report = run([make_topology(["planner"])], manifests=manifests, host_cpu_cores=1.0)

    assert "quotas" in report.checks()


def test_within_capacity_passes():
    manifests = {
        "planner": agent("planner", runtime={"resources": {"cpu": "1.0", "memory": "1Gi"}})
    }

    report = run([make_topology(["planner"])], manifests=manifests, host_cpu_cores=8.0)

    assert report.ok


# --- 결과 수집 ----------------------------------------------------------------


def test_every_failure_is_reported_together():
    """첫 실패에서 멈추면 운영자가 고칠 때마다 처음부터 다시 돌려야 한다."""
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "absent"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/absent@9.9.9"},
                "runtime": {"env_allowlist": ["MYSTERY_KEY"]},
            },
        )
    }
    topology = make_topology(["planner", "ghost-node"], connections=[("planner", "nowhere")])

    report = run([topology], manifests=manifests)

    assert set(report.checks()) >= {"agent_refs", "module_refs", "groups", "env_allowlist"}


def test_raise_if_failed_aborts_deployment():
    """미검증 상태로 컨테이너를 기동하지 않는다."""
    report = run([make_topology(["absent"])])

    with pytest.raises(MalkuthError) as exc_info:
        report.raise_if_failed()

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.details["failures"]


def test_raise_if_failed_is_silent_when_valid():
    run([make_topology(["planner"])]).raise_if_failed()


# --- 실제 레퍼런스 배포 --------------------------------------------------------


@pytest.mark.skipif(
    not (REPO_ROOT / "graphs" / "research-pipeline.yaml").exists(),
    reason="reference artifacts are not present on this branch",
)
def test_reference_deployment_passes_every_check():
    """실제 배포되는 아티팩트가 8항목을 통과해야 한다 — 검증기가 만든 예제만
    통과하면 아무것도 증명하지 못한다."""
    import yaml

    from malkuth.core.manifest import AgentManifest, GroupManifest

    def load(relative: str) -> dict:
        document: dict = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
        return document

    manifests = {
        name: AgentManifest.model_validate(load(f"agents/{name}/manifest.yaml"))
        for name in ("planner", "researcher", "writer")
    }
    topologies = [
        GraphTopology.model_validate(load(f"graphs/{name}.yaml"))
        for name in ("research-pipeline", "feed-monitor", "memory-maintenance")
    ]
    refs = {m.spec.promptset.ref for m in manifests.values()}
    refs |= {s.ref for m in manifests.values() for s in m.spec.skillsets}
    refs |= {s.ref for m in manifests.values() for s in m.spec.memory.spaces}

    report = validate_deployment(
        topologies,
        manifests=manifests,
        groups={"research": GroupManifest.model_validate(load("groups/research.yaml"))},
        resolvable_refs=refs,
        global_secrets={"ANTHROPIC_API_KEY"},
        a2a_port_range=(9100, 9199),
        host_cpu_cores=8.0,
    )

    assert report.ok, [f"{f.check}: {f.message}" for f in report.findings]
