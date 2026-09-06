"""What the repository declares — agents, graphs, modules, groups.

저장소에 선언된 것을 읽는 **한 곳**. CLI `status` 와 Control Plane 카탈로그 API 가
같은 코드를 쓴다 (#240) — 조회가 CLI 에만 묶여 있으면 UI 는 "무엇으로 조립할 수
있는가" 를 알 길이 없고, CLI 만 고치고 API 를 빠뜨리는 일이 생긴다.

목록은 **읽을 수 있는 것을 돌려주고 깨진 것은 이름을 대며 따로 보고한다** —
선언 하나가 깨졌다고 카탈로그 전체가 500 이 되면 운영자는 어느 파일인지 모른다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest, GroupManifest
from malkuth.modules.registry import ModuleRegistry, RegistryRoots
from malkuth.orchestrator.topology import GraphTopology

MODULE_TYPES: tuple[str, ...] = ("skillsets", "promptsets", "memorysets")
"""API 로 읽을 수 있는 모듈 종류 — v0.1 은 전부 읽기 전용이다 (04 Registry 2)."""


@dataclass(frozen=True)
class Problem:
    """A declaration that could not be read.

    읽지 못한 선언 하나 — **어느 파일이 왜** 인지가 전부다.
    """

    path: str
    code: str
    message: str


@dataclass(frozen=True)
class Listing[T: BaseModel]:
    """Parsed declarations plus the ones that failed to parse.

    파싱된 선언과 실패한 선언을 함께 담는다. 실패를 예외로 올리면 하나 때문에
    전체가 사라지고, 조용히 빼면 운영자가 빠진 줄 모른다.
    """

    items: dict[str, T] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping.

    YAML 문서를 읽습니다.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the file cannot be read or parsed.
    """
    from malkuth.config import config_error

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as err:
        raise config_error("cannot read yaml document", path=str(path)) from err

    if not isinstance(document, dict):
        raise config_error("yaml document must be a mapping", path=str(path))
    return document


def not_found(kind: str, name: str) -> MalkuthError:
    """없는 대상 — 04 의 공용 매핑이 404 로 옮긴다."""
    return MalkuthError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=f"unknown {kind}: {name}",
        details={"kind": kind, "name": name},
    )


def _invalid(path: Path, err: ValidationError) -> MalkuthError:
    """스키마 위반을 파일 이름과 함께 — 어느 필드가 왜 인지까지."""
    return MalkuthError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message="declaration failed schema validation",
        details={
            "path": str(path),
            "errors": [
                {"field": ".".join(str(loc) for loc in e["loc"]), "problem": e["msg"]}
                for e in err.errors()
            ],
        },
    )


def _parse[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        return model.model_validate(load_yaml(path))
    except ValidationError as err:
        raise _invalid(path, err) from err


def _mismatch(path: Path, *, declared: str, located: str) -> MalkuthError:
    """선언된 이름과 파일 위치가 다르다 — 04 Registry 3 의 integrity 위반."""
    return MalkuthError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message="declared name does not match its location",
        details={"path": str(path), "declared": declared, "located": located},
    )


def _checked[T: BaseModel](
    path: Path, model: type[T], key: Callable[[T], str], *, located: str
) -> T:
    parsed = _parse(path, model)
    if key(parsed) != located:
        raise _mismatch(path, declared=key(parsed), located=located)
    return parsed


def _collect[T: BaseModel](
    paths: Iterable[Path],
    model: type[T],
    key: Callable[[T], str],
    *,
    located: Callable[[Path], str],
) -> Listing[T]:
    """파싱해 이름으로 묶는다 — **위치가 곧 정체성**이다.

    목록의 키는 파일 위치(디렉토리/파일명)에서 오고, 선언 안의 이름이 그것과
    다르면 깨진 선언으로 보고한다. 그래야 목록의 키로 단건 조회가 되고,
    같은 이름을 선언한 두 파일이 서로를 조용히 덮어쓰지 못한다.
    """
    items: dict[str, T] = {}
    problems: list[Problem] = []
    for path in sorted(paths):
        try:
            parsed = _parse(path, model)
            if key(parsed) != located(path):
                raise _mismatch(path, declared=key(parsed), located=located(path))
        except MalkuthError as err:
            problems.append(Problem(path=str(path), code=err.code, message=err.message))
            continue
        items[located(path)] = parsed
    return Listing(items=items, problems=tuple(problems))


@dataclass(frozen=True)
class ModuleListing:
    """Published module versions per name, plus the ones that failed integrity."""

    items: dict[str, tuple[str, ...]] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class Catalog:
    """Read access to everything the repository declares.

    저장소 선언에 대한 읽기 접근. 경로 레이아웃은 `RegistryRoots` 가 정한다 —
    여기서 경로를 하드코딩하지 않는다 (04 Registry 1).
    """

    roots: RegistryRoots

    @classmethod
    def under(cls, base: Path) -> Catalog:
        """레포 루트 기준 기본 배치."""
        return cls(roots=RegistryRoots.under(base))

    @classmethod
    def from_config(cls, roots: Mapping[str, str] | Any, *, base: Path) -> Catalog:
        """설정의 상대 경로 루트를 `base` 기준으로 해석한다.

        설정(`RegistryConfig.roots`)은 문자열이고 상대 경로다 — control plane 은
        그것을 자기 작업 디렉토리가 아니라 **레포 루트** 기준으로 봐야 한다.
        """
        values = roots if isinstance(roots, Mapping) else roots.model_dump()
        return cls(
            roots=RegistryRoots(
                **{
                    name: (base / Path(values[name])).resolve()
                    for name in RegistryRoots.__dataclass_fields__
                }
            )
        )

    # --- agents ----------------------------------------------------------------

    def agents(self) -> Listing[AgentManifest]:
        return _collect(
            self.roots.agents.glob("*/manifest.yaml"),
            AgentManifest,
            lambda m: m.name,
            located=lambda path: path.parent.name,
        )

    def agent(self, name: str) -> AgentManifest:
        path = self.roots.agents / name / "manifest.yaml"
        if not path.is_file():
            raise not_found("agent", name)
        return _checked(path, AgentManifest, lambda m: m.name, located=name)

    # --- graphs ----------------------------------------------------------------

    def graphs(self) -> Listing[GraphTopology]:
        return _collect(
            self.roots.graphs.glob("*.yaml"),
            GraphTopology,
            lambda g: g.metadata.name,
            located=lambda path: path.stem,
        )

    def graph(self, name: str) -> GraphTopology:
        path = self.roots.graphs / f"{name}.yaml"
        if not path.is_file():
            raise not_found("graph", name)
        return _checked(path, GraphTopology, lambda g: g.metadata.name, located=name)

    # --- groups ----------------------------------------------------------------

    def groups(self) -> Listing[GroupManifest]:
        return _collect(
            self.roots.groups.glob("*.yaml"),
            GroupManifest,
            lambda g: g.metadata.name,
            located=lambda path: path.stem,
        )

    def group(self, name: str) -> GroupManifest:
        path = self.roots.groups / f"{name}.yaml"
        if not path.is_file():
            raise not_found("group", name)
        return _checked(path, GroupManifest, lambda g: g.metadata.name, located=name)

    # --- modules ---------------------------------------------------------------

    def module_refs(self) -> frozenset[str]:
        """게시된 모든 모듈 ref — 배포 검증의 `resolvable_refs` 입력.

        **무결성을 통과한 것만** 게시한다. 디렉토리만 있고 문서가 깨진 ref 를
        해석 가능으로 넘기면 배포 검증은 통과하고 로드에서야 실패한다.
        """
        refs: set[str] = set()
        for module_type in MODULE_TYPES:
            for name, versions in self.modules(module_type).items.items():
                refs.update(f"{module_type}/{name}@{version}" for version in versions)
        return frozenset(refs)

    def modules(self, module_type: str) -> ModuleListing:
        """타입별 모듈 이름 → 게시된 버전들 (+ 깨진 것).

        디렉토리 구조가 ``{type}/{name}/{version}/`` 이므로 경로에서 후보를 찾고,
        각 문서를 실제로 읽어 ``kind``/``name``/``version`` 이 맞는지 본다 —
        빈 디렉토리나 깨진 문서는 게시된 것이 아니다.
        """
        if module_type not in MODULE_TYPES:
            raise not_found("module type", module_type)
        type_root = self.roots.for_type(module_type)
        if not type_root.is_dir():
            return ModuleListing()
        registry = ModuleRegistry(self.roots)
        found: dict[str, list[str]] = {}
        problems: list[Problem] = []
        for version_dir in sorted(type_root.glob("*/*")):
            if not version_dir.is_dir():
                continue
            ref = f"{module_type}/{version_dir.parent.name}@{version_dir.name}"
            try:
                registry.load_document(ref)
            except MalkuthError as err:
                problems.append(Problem(path=str(version_dir), code=err.code, message=err.message))
                continue
            found.setdefault(version_dir.parent.name, []).append(version_dir.name)
        return ModuleListing(
            items={name: tuple(versions) for name, versions in found.items()},
            problems=tuple(problems),
        )

    def module(self, module_type: str, name: str, version: str) -> dict[str, Any]:
        """모듈 선언 문서 — 무결성 검사(`kind`/`name`/`version` 일치)를 거친다."""
        if module_type not in MODULE_TYPES:
            raise not_found("module type", module_type)
        ref = f"{module_type}/{name}@{version}"
        try:
            _path, document = ModuleRegistry(self.roots).load_document(ref)
        except MalkuthError as err:
            if err.code == ErrorCode.MOD_001:
                raise not_found("module", ref) from err
            raise
        return document


__all__ = [
    "MODULE_TYPES",
    "Catalog",
    "Listing",
    "ModuleListing",
    "Problem",
    "load_yaml",
    "not_found",
]
