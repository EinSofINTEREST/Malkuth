"""Writing graphs and agent manifests — validated before anything touches disk.

UI 가 조립한 결과를 **저장할 통로**와, 저장 전에 **검증할 통로** (#242).
`DeployValidator` 는 있었지만 CLI 전용이었다 — 여기서는 저장된 선언과 초안을 합쳐
같은 8항목 검증을 돌리고, 통과한 것만 쓴다 (01 Contract Validation).

쓰지 않는 것: 모듈(skillset / promptset / memoryset). v0.1 에서 게시된 모듈은
불변이다 (04 Registry 2) — 편집 가능한 것은 그래프와 에이전트 매니페스트뿐이다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from malkuth.catalog import MODULE_TYPES, Catalog, not_found
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.deploy import Finding, ValidationReport, validate_deployment
from malkuth.orchestrator.topology import GraphTopology

InUse = Callable[[str, str], bool]
"""``(kind, name)`` 이 지금 배포 중인가 — 배포 lifecycle(#243) 이 채운다. 없으면 항상 False."""


def _version_tuple(version: str) -> tuple[int, ...]:
    # SemVer 검증은 모델이 이미 했다 — 여기서는 비교만
    return tuple(int(part) for part in version.split("."))


def _serialize(model: BaseModel) -> str:
    """사람이 읽는 YAML — 키 순서 ``apiVersion → kind → metadata → spec`` (07 Manifest YAML)."""
    document = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _round_trips[T: BaseModel](model: T, kind: type[T]) -> None:
    """쓰기 **전에** 직렬화 → 파싱이 같은 모델을 내는지 본다 — 안 되면 파일이 생기지 않는다."""
    again = kind.model_validate(yaml.safe_load(_serialize(model)))
    if again != model:
        raise MalkuthError(
            category=ErrorCategory.INTERNAL,
            code=ErrorCode.INTERNAL_001,
            message="declaration does not survive a serialization round-trip",
            details={"kind": kind.__name__},
        )


def _rejected(message: str, **details: Any) -> MalkuthError:
    return MalkuthError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=message,
        details=details,
    )


def _version_conflict(kind: str, name: str, *, existing: str, proposed: str) -> MalkuthError:
    """내용이 바뀌면 버전이 올라야 한다 (02 Manifest Rules 2 / 04 Graph Rules)."""
    return MalkuthError(
        category=ErrorCategory.MODULE,
        code=ErrorCode.MOD_002,
        message=f"{kind} changed without a version bump",
        details={"kind": kind, "name": name, "existing": existing, "proposed": proposed},
    )


@dataclass(frozen=True)
class Author:
    """Validates and writes graphs and agent manifests.

    그래프와 매니페스트를 검증하고 씁니다. 읽기는 `Catalog` 가, 쓰기는 여기가 —
    한 파일이 두 곳에서 쓰이면 한쪽이 다른 쪽의 규칙을 모른다.

    Attributes:
        catalog: What is already declared — 검증의 바탕이자 쓰기의 대상.
        a2a_port_range: The runtime's allocatable range, for the port check.
        in_use: Whether a declaration is currently deployed (#243). 배포 중인
            것을 지우거나 덮어쓰면 실행 중 run 의 계약이 발밑에서 바뀐다.
    """

    catalog: Catalog
    a2a_port_range: tuple[int, int] | None = None
    in_use: InUse | None = None

    # --- 검증 ----------------------------------------------------------------

    def validate(
        self,
        *,
        graphs: Sequence[GraphTopology] = (),
        agents: Sequence[AgentManifest] = (),
        replacing: Path | None = None,
        with_saved_graphs: bool = True,
    ) -> ValidationReport:
        """Validate drafts together with everything already saved.

        초안을 **저장된 것과 함께** 검증합니다 — 초안 그래프가 기존 에이전트를
        참조하는 것이 일반적이고, 초안 에이전트는 기존 그래프를 깨뜨릴 수 있습니다.
        같은 이름의 초안은 저장된 것을 덮습니다.

        ``with_saved_graphs=False`` 면 **주어진 그래프만** 검증한다 — CLI 의 `deploy` /
        `validate` 는 지목한 그래프만 판정해 왔다. 다만 에이전트/그룹/모듈 검사는
        저장소 전체를 본다 (그 검사들은 애초에 그래프 단위가 아니다).

        읽지 못한 선언은 검증에서 빠진 것이지 통과한 것이 아니다 — finding 으로 합친다.
        단 ``replacing`` 경로의 문제는 뺀다: 그 파일은 이번 저장이 덮어쓰는 것이라,
        깨진 파일을 고치는 저장이 그 파일 때문에 거절되면 고칠 길이 없다.
        """
        saved_agents, saved_groups = self.catalog.agents(), self.catalog.groups()
        manifests = {**saved_agents.items, **{m.name: m for m in agents}}
        saved_graphs = self.catalog.graphs()
        drafted = {g.metadata.name for g in graphs}
        others = saved_graphs.items.items() if with_saved_graphs else ()
        topologies = [*graphs, *(g for n, g in others if n not in drafted)]

        report = validate_deployment(
            topologies,
            manifests=manifests,
            groups=saved_groups.items,
            resolvable_refs=self.catalog.module_refs(),
            global_secrets=(
                frozenset(saved_groups.items["global"].spec.secrets)
                if "global" in saved_groups.items
                else ()
            ),
            a2a_port_range=self.a2a_port_range,
        )
        problems = [
            *saved_agents.problems,
            *saved_groups.problems,
            *saved_graphs.problems,
            *(p for t in MODULE_TYPES for p in self.catalog.modules(t).problems),
        ]
        target = str(replacing) if replacing is not None else None
        broken = [
            Finding(
                check="catalog", code=ErrorCode(p.code), message=p.message, details={"path": p.path}
            )
            for p in problems
            if p.path != target
        ]
        return ValidationReport(findings=(*broken, *report.findings))

    # --- 그래프 ---------------------------------------------------------------

    def save_graph(self, name: str, topology: GraphTopology) -> Path:
        """Validate and write one graph.

        검증을 통과한 그래프만 씁니다. 경로의 이름과 선언의 이름이 같아야 합니다 —
        카탈로그에서 **위치가 정체성**이기 때문입니다.
        """
        if topology.metadata.name != name:
            raise _rejected(
                "graph name in the path does not match the declaration",
                path_name=name,
                declared=topology.metadata.name,
            )
        existing = self._existing(name, self.catalog.graph)
        if existing == topology:
            return self._graph_path(name)  # 멱등 — 같은 것을 다시 쓰지 않는다
        if existing is not None:
            self._require_bump("graph", name, existing.metadata.version, topology.metadata.version)
        self._refuse_if_in_use("graph", name)
        self._require_ok(self.validate(graphs=[topology], replacing=self._graph_path(name)))
        _round_trips(topology, GraphTopology)
        return self._write(self._graph_path(name), topology)

    def delete_graph(self, name: str) -> None:
        path = self._graph_path(name)
        if not path.is_file():
            raise not_found("graph", name)
        self._refuse_if_in_use("graph", name)
        path.unlink()

    # --- 에이전트 -----------------------------------------------------------

    def save_agent(self, name: str, manifest: AgentManifest) -> Path:
        """Validate and write one agent manifest.

        에이전트 자체의 계약(모듈 ref / 그룹 / env_allowlist / quota)과, 이 에이전트를
        참조하는 **모든 저장된 그래프**가 여전히 통과하는지를 함께 봅니다.
        """
        if manifest.name != name:
            raise _rejected(
                "agent name in the path does not match the declaration",
                path_name=name,
                declared=manifest.name,
            )
        existing = self._existing(name, self.catalog.agent)
        if existing == manifest:
            return self._agent_path(name)
        if existing is not None:
            self._require_bump("agent", name, existing.metadata.version, manifest.metadata.version)
        self._refuse_if_in_use("agent", name)
        self._require_ok(self.validate(agents=[manifest], replacing=self._agent_path(name)))
        _round_trips(manifest, AgentManifest)
        return self._write(self._agent_path(name), manifest)

    def delete_agent(self, name: str) -> None:
        path = self._agent_path(name)
        if not path.is_file():
            raise not_found("agent", name)
        referenced_by = sorted(
            graph_name
            for graph_name, graph in self.catalog.graphs().items.items()
            if any(
                node.agent is not None and _agent_of(node.agent) == name
                for node in graph.spec.nodes
            )
        )
        if referenced_by:
            # 참조를 남긴 채 지우면 그 그래프는 다음 검증에서야 깨진다 — 지금 막는다
            raise _rejected(
                "agent is referenced by graphs and cannot be deleted",
                agent=name,
                referenced_by=referenced_by,
            )
        self._refuse_if_in_use("agent", name)
        path.unlink()

    # --- 내부 ---------------------------------------------------------------

    def _graph_path(self, name: str) -> Path:
        return self.catalog.roots.graphs / f"{name}.yaml"

    def _agent_path(self, name: str) -> Path:
        return self.catalog.roots.agents / name / "manifest.yaml"

    def _existing[T: BaseModel](self, name: str, read: Callable[[str], T]) -> T | None:
        """저장된 같은 이름 — 없으면 None.

        읽을 수는 있는데 깨져 있으면 **덮어쓰기를 허용**한다: 버전을 비교할 대상이
        없고, 깨진 파일을 고치는 길이 API 에 있어야 한다. 깨진 사실은 카탈로그가
        이미 `problems` 로 보고하고 있다.
        """
        try:
            return read(name)
        except MalkuthError:
            return None

    @staticmethod
    def _require_bump(kind: str, name: str, existing: str, proposed: str) -> None:
        if _version_tuple(proposed) <= _version_tuple(existing):
            raise _version_conflict(kind, name, existing=existing, proposed=proposed)

    def _refuse_if_in_use(self, kind: str, name: str) -> None:
        if self.in_use is not None and self.in_use(kind, name):
            raise _rejected(
                f"{kind} is currently deployed — tear the deployment down first",
                kind=kind,
                name=name,
            )

    @staticmethod
    def _require_ok(report: ValidationReport) -> None:
        if report.ok:
            return
        raise MalkuthError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_001,
            message="declaration failed deployment validation",
            details={
                "findings": [
                    {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
                    for f in report.findings
                ]
            },
        )

    @staticmethod
    def _write(path: Path, model: BaseModel) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize(model), encoding="utf-8")
        return path


def _agent_of(ref: str) -> str:
    """``agents/{name}@{version}`` → name."""
    return ref.split("/", 1)[1].split("@", 1)[0]


__all__ = ["Author", "InUse"]
