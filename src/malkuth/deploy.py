"""Deploy-time contract validation.

배포 시점 계약 검증. **하나라도 실패하면 컨테이너를 기동하지 않는다**
(01 Contract Validation at Deploy Time).

검증 결과는 항목별로 모아 한 번에 보고한다 — 첫 실패에서 멈추면 운영자가
고칠 때마다 처음부터 다시 돌려야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import RESERVED_GLOBAL_GROUP
from malkuth.orchestrator.topology import GraphMode, validate_topology
from malkuth.runtime.quota import check_group_quota, check_host_capacity

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from malkuth.core.manifest import AgentManifest, GroupManifest
    from malkuth.orchestrator.topology import GraphTopology

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Finding:
    """One failed contract check.

    실패한 검증 하나. ``code`` 로 어느 카테고리의 문제인지 즉시 알 수 있다.
    """

    check: str
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """The outcome of every deploy check.

    전체 검증 결과. 통과 여부와 함께 **모든** 실패 항목을 담는다.
    """

    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """전부 통과했는지."""
        return not self.findings

    def codes(self) -> tuple[ErrorCode, ...]:
        """실패한 항목의 에러 코드."""
        return tuple(f.code for f in self.findings)

    def checks(self) -> tuple[str, ...]:
        """실패한 검증 이름."""
        return tuple(f.check for f in self.findings)

    def raise_if_failed(self) -> None:
        """Abort deployment when any check failed.

        하나라도 실패했으면 배포를 중단합니다 — 미검증 상태로 컨테이너를
        기동하지 않습니다.

        Raises:
            MalkuthError: CONFIG/``CFG_001`` carrying every finding.
        """
        if self.ok:
            return
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="deployment validation failed",
            details={
                "failures": [
                    {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
                    for f in self.findings
                ]
            },
        )


def _agent_name(ref: str) -> str:
    """``agents/{name}@{version}`` 에서 이름만 뽑는다."""
    _, _, remainder = ref.partition("/")
    name, _, _ = remainder.partition("@")
    return name


@dataclass
class DeployValidator:
    """Runs every deploy-time contract check.

    배포 시점 계약 검증 전체를 수행한다. 각 검증은 독립적으로 실행되어
    실패가 뒤의 검증을 가리지 않는다.

    Attributes:
        manifests: Agent name to its validated manifest.
        groups: Group name to its manifest.
        resolvable_refs: Module refs the registry can resolve.
        available_secrets: Secret keys resolvable per scope.
    """

    manifests: Mapping[str, AgentManifest]
    groups: Mapping[str, GroupManifest] = field(default_factory=dict)
    resolvable_refs: frozenset[str] = frozenset()
    local_secrets: Mapping[str, frozenset[str]] = field(default_factory=dict)
    global_secrets: frozenset[str] = frozenset()
    host_cpu_cores: float | None = None
    host_memory_bytes: int | None = None
    a2a_port_range: tuple[int, int] | None = None

    def validate(self, topologies: Sequence[GraphTopology]) -> ValidationReport:
        """Run all eight checks across the graphs being deployed.

        배포 대상 그래프 전체에 8항목 검증을 수행합니다.

        Args:
            topologies: The graph topologies about to be deployed.

        Returns:
            The report — ``ok`` is False if any check failed.
        """
        findings: list[Finding] = []

        for topology in topologies:
            findings.extend(self._check_agent_refs(topology))
            findings.extend(self._check_connections(topology))
            findings.extend(self._check_mode_rules(topology))

        findings.extend(self._check_module_refs())
        findings.extend(self._check_groups())
        findings.extend(self._check_env_allowlists())
        findings.extend(self._check_a2a_ports(topologies))
        findings.extend(self._check_quotas())

        report = ValidationReport(findings=tuple(findings))
        if report.ok:
            log.info("deployment validation passed", graphs=len(topologies))
        else:
            log.error(
                "deployment validation failed",
                graphs=len(topologies),
                failures=len(report.findings),
            )
        return report

    # --- 1. agent ref 해석 ------------------------------------------------

    def _check_agent_refs(self, topology: GraphTopology) -> list[Finding]:
        """그래프의 모든 agent ref 가 존재하는 manifest 를 가리키는지."""
        findings = []
        for node in topology.spec.nodes:
            if node.agent is None:
                continue
            name = _agent_name(node.agent)
            if name not in self.manifests:
                findings.append(
                    Finding(
                        check="agent_refs",
                        code=ErrorCode.MOD_001,
                        message=f"graph node references an unknown agent: {node.agent}",
                        details={
                            "graph": topology.metadata.name,
                            "node_id": node.id,
                            "module_ref": node.agent,
                        },
                    )
                )
        return findings

    # --- 2. 모듈 ref 해석 -------------------------------------------------

    def _check_module_refs(self) -> list[Finding]:
        """manifest 가 선언한 모든 모듈 ref 가 registry 에서 해석되는지."""
        findings = []
        for name, manifest in self.manifests.items():
            refs = [manifest.spec.promptset.ref]
            refs.extend(s.ref for s in manifest.spec.skillsets)
            refs.extend(s.ref for s in manifest.spec.memory.spaces)

            for ref in refs:
                if ref not in self.resolvable_refs:
                    findings.append(
                        Finding(
                            check="module_refs",
                            code=ErrorCode.MOD_001,
                            message=f"module ref cannot be resolved: {ref}",
                            details={"agent": name, "module_ref": ref},
                        )
                    )
        return findings

    # --- 3. 그룹 소속 -----------------------------------------------------

    def _check_groups(self) -> list[Finding]:
        """선언된 그룹이 존재하는지, 예약 그룹을 직접 선언하지 않았는지."""
        findings = []
        for name, manifest in self.manifests.items():
            declared = manifest.metadata.group
            if declared is None:
                continue
            if declared == RESERVED_GLOBAL_GROUP:
                findings.append(
                    Finding(
                        check="groups",
                        code=ErrorCode.CFG_002,
                        message="agents must not declare the reserved global group",
                        details={"agent": name, "group": declared},
                    )
                )
                continue
            if declared not in self.groups:
                findings.append(
                    Finding(
                        check="groups",
                        code=ErrorCode.CFG_002,
                        message=f"agent belongs to an unknown group: {declared}",
                        details={"agent": name, "group": declared},
                    )
                )
        return findings

    # --- 4. env_allowlist 해석 --------------------------------------------

    def _check_env_allowlists(self) -> list[Finding]:
        """각 env 키가 local > group > global 체인에서 해석되는지."""
        findings = []
        for name, manifest in self.manifests.items():
            local = self.local_secrets.get(name, frozenset())
            # metadata.group 을 쓴다 — manifest.group 은 미선언 시 예약 그룹
            # 'global' 로 기본값 처리되므로, 그걸 쓰면 미소속 에이전트가
            # groups['global'] 의 secrets 로 통과한다. 런타임 해석
            # (ScopedSecrets.for_agent) 은 미소속을 어떤 그룹 멤버로도 보지
            # 않으므로 배포 검증만 통과하고 기동에서 CFG_002 로 실패한다
            declared_group = manifest.metadata.group
            group = self.groups.get(declared_group) if declared_group else None
            group_keys = frozenset(group.spec.secrets) if group else frozenset()

            for key in manifest.spec.runtime.env_allowlist:
                if key in local or key in group_keys or key in self.global_secrets:
                    continue
                findings.append(
                    Finding(
                        check="env_allowlist",
                        code=ErrorCode.CFG_002,
                        message=f"env key cannot be resolved in any scope: {key}",
                        details={"agent": name, "env_key": key, "group": declared_group},
                    )
                )
        return findings

    # --- 5. connections --------------------------------------------------

    def _check_connections(self, topology: GraphTopology) -> list[Finding]:
        """allowlist 의 caller/callee 가 모두 그래프 노드인지, callee 가 A2A 를 여는지."""
        findings = []
        node_ids = {node.id for node in topology.spec.nodes}

        for connection in topology.spec.connections:
            for role, node_id in (("caller", connection.caller), ("callee", connection.callee)):
                if node_id not in node_ids:
                    findings.append(
                        Finding(
                            check="connections",
                            code=ErrorCode.GRAPH_001,
                            message=f"connection {role} is not a graph node: {node_id}",
                            details={"graph": topology.metadata.name, role: node_id},
                        )
                    )
            findings.extend(self._check_callee_accepts_a2a(topology, connection))
        return findings

    def _check_callee_accepts_a2a(self, topology: GraphTopology, connection: Any) -> list[Finding]:
        """Peer 호출을 받으려면 callee 가 ``a2a.enabled`` 여야 한다 (03 Rule 5)."""
        node = next((n for n in topology.spec.nodes if n.id == connection.callee), None)
        if node is None or node.agent is None:
            return []

        manifest = self.manifests.get(_agent_name(node.agent))
        if manifest is None or manifest.spec.a2a.enabled:
            return []

        return [
            Finding(
                check="connections",
                code=ErrorCode.A2A_004,
                message=f"connection callee does not enable a2a: {connection.callee}",
                details={
                    "graph": topology.metadata.name,
                    "a2a_caller": connection.caller,
                    "a2a_callee": connection.callee,
                },
            )
        ]

    # --- 6. mode 별 토폴로지 규칙 ------------------------------------------

    def _check_mode_rules(self, topology: GraphTopology) -> list[Finding]:
        """mission 은 END 도달, service 는 idle 정책 — 검증기에 위임한다."""
        try:
            validate_topology(topology)
        except MalkuthError as err:
            return [
                Finding(
                    check="mode_rules",
                    code=ErrorCode(err.code),
                    message=err.message,
                    details={"graph": topology.metadata.name, **err.details},
                )
            ]
        return []

    # --- 7. A2A 포트 수용량 --------------------------------------------

    def _check_a2a_ports(self, topologies: Sequence[GraphTopology]) -> list[Finding]:
        """Verify the port range can seat every A2A-enabled agent.

        포트는 manifest 가 아니라 **runtime 이 범위에서 할당**하므로
        (03 Rule 2), 검증할 것은 선언 충돌이 아니라 **수용량**이다 —
        범위가 모자라면 마지막 에이전트가 기동에 실패한다.
        """
        exposed = sorted(
            name for name, manifest in self.manifests.items() if manifest.spec.a2a.enabled
        )
        if not exposed or self.a2a_port_range is None:
            return []

        low, high = self.a2a_port_range
        available = high - low + 1
        if len(exposed) <= available:
            return []

        return [
            Finding(
                check="a2a_ports",
                code=ErrorCode.CFG_002,
                message="a2a port range cannot seat every exposed agent",
                details={
                    "port_range": [low, high],
                    "available": available,
                    "required": len(exposed),
                    "agents": exposed,
                },
            )
        ]

    # --- 8. quota ---------------------------------------------------------

    def _check_quotas(self) -> list[Finding]:
        """그룹별 합계가 quota 이내인지, 전체가 호스트 한도 이내인지."""
        findings = []

        for group_name, group in self.groups.items():
            members = [m for m in self.manifests.values() if m.group == group_name]
            try:
                check_group_quota(group, members)
            except MalkuthError as err:
                findings.append(
                    Finding(
                        check="quotas",
                        code=ErrorCode(err.code),
                        message=err.message,
                        details={"group": group_name, **err.details},
                    )
                )

        try:
            check_host_capacity(
                self.manifests.values(),
                cpu_cores=self.host_cpu_cores,
                memory_bytes=self.host_memory_bytes,
            )
        except MalkuthError as err:
            findings.append(
                Finding(
                    check="quotas",
                    code=ErrorCode(err.code),
                    message=err.message,
                    details=dict(err.details),
                )
            )
        return findings


def validate_deployment(
    topologies: Iterable[GraphTopology],
    *,
    manifests: Mapping[str, AgentManifest],
    groups: Mapping[str, GroupManifest] | None = None,
    resolvable_refs: Iterable[str] = (),
    local_secrets: Mapping[str, frozenset[str]] | None = None,
    global_secrets: Iterable[str] = (),
    host_cpu_cores: float | None = None,
    host_memory_bytes: int | None = None,
    a2a_port_range: tuple[int, int] | None = None,
) -> ValidationReport:
    """Validate a deployment against all eight contract checks.

    배포를 8항목 계약 검증으로 확인합니다.

    Args:
        topologies: Graphs about to be deployed.
        manifests: Agent name to manifest.
        groups: Group name to manifest.
        resolvable_refs: Module refs the registry resolves.
        local_secrets: Agent name to its locally available secret keys.
        global_secrets: Globally available secret keys.
        host_cpu_cores: Host CPU ceiling.
        host_memory_bytes: Host memory ceiling.
        a2a_port_range: The runtime's allocatable A2A port range.

    Returns:
        The validation report.
    """
    validator = DeployValidator(
        manifests=manifests,
        groups=dict(groups or {}),
        resolvable_refs=frozenset(resolvable_refs),
        local_secrets=dict(local_secrets or {}),
        global_secrets=frozenset(global_secrets),
        host_cpu_cores=host_cpu_cores,
        host_memory_bytes=host_memory_bytes,
        a2a_port_range=a2a_port_range,
    )
    return validator.validate(list(topologies))


__all__ = [
    "DeployValidator",
    "Finding",
    "GraphMode",
    "ValidationReport",
    "validate_deployment",
]
