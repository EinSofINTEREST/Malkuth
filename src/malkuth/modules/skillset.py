"""Skillset schema and loader.

스킬셋 선언 스키마와 로더. 스킬셋 코드는 소유 에이전트의 컨테이너 안에서만
import/실행되며, tool 스키마는 함수 시그니처에서 자동 생성된다.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import SemVer
from malkuth.core.skill import SkillSpec, build_spec, get_spec
from malkuth.modules.registry import ModulePath, ModuleRegistry, validation_error

DEFAULT_SKILL_TIMEOUT_S = 60.0


class SkillDeclaration(BaseModel):
    """A single skill entry in ``skillset.yaml``.

    스킬셋의 개별 skill 선언.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    entrypoint: str
    description: str | None = None
    timeout_s: float = DEFAULT_SKILL_TIMEOUT_S

    @field_validator("entrypoint")
    @classmethod
    def _valid_entrypoint(cls, value: str) -> str:
        """entrypoint 는 ``module:function`` 형식이어야 한다."""
        if value.count(":") != 1:
            raise ValueError("entrypoint must be in 'module:function' format")
        module, function = value.split(":")
        if not module or not function:
            raise ValueError("entrypoint must be in 'module:function' format")
        return value

    @property
    def module_name(self) -> str:
        """entrypoint 의 모듈 부분."""
        return self.entrypoint.split(":")[0]

    @property
    def function_name(self) -> str:
        """entrypoint 의 함수 부분."""
        return self.entrypoint.split(":")[1]


class SkillsetRequires(BaseModel):
    """Skillset requirements checked against the agent manifest.

    스킬셋 요구사항 — 배포 검증에서 에이전트 manifest 와 대조된다.
    """

    model_config = ConfigDict(frozen=True)

    env: tuple[str, ...] = ()
    python: str | None = None


class SkillsetSpec(BaseModel):
    """Skillset body."""

    model_config = ConfigDict(frozen=True)

    skills: tuple[SkillDeclaration, ...]
    requires: SkillsetRequires = Field(default_factory=SkillsetRequires)

    @field_validator("skills")
    @classmethod
    def _unique_names(cls, value: tuple[SkillDeclaration, ...]) -> tuple[SkillDeclaration, ...]:
        """스킬셋 내 tool 이름 중복 금지."""
        if not value:
            raise ValueError("skillset must declare at least one skill")
        names = [s.name for s in value]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate skill name: {sorted(duplicates)}")
        return value


class SkillsetMetadata(BaseModel):
    """Skillset metadata."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: SemVer
    description: str | None = None


class SkillsetManifest(BaseModel):
    """``skillset.yaml`` document."""

    model_config = ConfigDict(frozen=True)

    api_version: Literal["malkuth/v1"] = Field(alias="apiVersion")
    kind: Literal["Skillset"]
    metadata: SkillsetMetadata
    spec: SkillsetSpec


class LoadedSkill(BaseModel):
    """A skill bound to its callable and derived tool schema.

    로드된 skill — 선언, 실제 함수, 그리고 시그니처에서 도출된 tool 스키마.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    declaration: SkillDeclaration
    spec: SkillSpec
    fn: Callable[..., Any]

    @property
    def name(self) -> str:
        """tool 이름 — 선언된 이름이 계약이다."""
        return self.declaration.name

    @property
    def timeout_s(self) -> float:
        """tool 실행 상한."""
        return self.declaration.timeout_s


class LoadedSkillset(BaseModel):
    """A fully loaded skillset.

    로드 완료된 스킬셋.
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    manifest: SkillsetManifest
    skills: tuple[LoadedSkill, ...]

    def tools(self) -> tuple[SkillSpec, ...]:
        """Tool schemas exposed to the model.

        모델에게 노출되는 tool 스키마 목록입니다.
        """
        return tuple(s.spec for s in self.skills)

    def get(self, name: str) -> LoadedSkill:
        """Look up a loaded skill by tool name.

        tool 이름으로 skill 을 조회합니다.

        Raises:
            MalkuthError: MODULE/``MOD_001`` if the skill is not present.
        """
        for item in self.skills:
            if item.name == name:
                return item
        raise MalkuthError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_001,
            message=f"skill not found in skillset: {name}",
            details={"skillset": self.ref, "tool": name},
        )

    @property
    def required_env(self) -> tuple[str, ...]:
        """스킬셋이 요구하는 env 키."""
        return self.manifest.spec.requires.env


class SkillsetLoader:
    """Loads skillsets through the registry.

    레지스트리를 통해 스킬셋을 로드한다. 스킬 모듈은 스킬셋 루트를 기준으로
    격리 import 되며, 스킬셋 간 import 는 허용하지 않는다.
    """

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    def load(self, ref: str) -> LoadedSkillset:
        """Load a skillset and bind its skill functions.

        스킬셋을 로드하고 skill 함수를 바인딩합니다.

        Args:
            ref: Skillset reference (``skillsets/{name}@{version}``).

        Returns:
            The loaded skillset with derived tool schemas.

        Raises:
            MalkuthError: MODULE/``MOD_001`` if the ref cannot be resolved,
                ``MOD_003`` if the declaration or an entrypoint is invalid.
        """
        path, document = self._registry.load_document(ref)
        try:
            manifest = SkillsetManifest.model_validate(document)
        except ValidationError as err:
            raise validation_error(ref, err) from err

        skills = tuple(self._bind(declaration, path, ref) for declaration in manifest.spec.skills)
        return LoadedSkillset(ref=ref, manifest=manifest, skills=skills)

    def _bind(self, declaration: SkillDeclaration, path: ModulePath, ref: str) -> LoadedSkill:
        """선언된 entrypoint 를 실제 함수로 해석하고 스키마를 도출한다."""
        module = _import_module(declaration.module_name, path, ref)
        fn = getattr(module, declaration.function_name, None)
        if fn is None:
            raise MalkuthError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_003,
                message=(f"skill entrypoint not found: {declaration.entrypoint}"),
                details={"skillset": ref, "skill": declaration.name},
            )

        spec = get_spec(fn)
        if spec is None:
            try:
                spec = build_spec(fn, name=declaration.name)
            except ValueError as err:
                raise MalkuthError(
                    category=ErrorCategory.MODULE,
                    code=ErrorCode.MOD_003,
                    message=f"invalid skill '{declaration.name}': {err}",
                    details={"skillset": ref, "skill": declaration.name},
                ) from err

        # 선언된 이름이 계약이다 — 함수명이 달라도 선언 이름으로 노출한다
        if spec.name != declaration.name:
            spec = spec.model_copy(update={"name": declaration.name})
        if declaration.description:
            spec = spec.model_copy(update={"description": declaration.description})

        return LoadedSkill(declaration=declaration, spec=spec, fn=fn)


def _register_packages(prefix: str, module_name: str, path: ModulePath) -> None:
    """스킬셋 루트와 그 하위 패키지를 sys.modules 에 등록한다.

    격리 import 로 로드된 모듈이 상대 import (``from .util import ...``) 와
    패키지 내부 절대 import 를 쓸 수 있으려면, 중간 패키지가 ``__path__`` 를 갖고
    등록돼 있어야 한다.
    """
    package = prefix
    directory = path.root
    if package not in sys.modules:
        sys.modules[package] = _make_package(package, directory)

    for part in module_name.split(".")[:-1]:
        directory = directory / part
        parent, package = package, f"{package}.{part}"
        if package not in sys.modules:
            sys.modules[package] = _make_package(package, directory)
        setattr(sys.modules[parent], part, sys.modules[package])


def _make_package(name: str, directory: Path) -> ModuleType:
    """``__path__`` 를 가진 빈 패키지 모듈을 만든다."""
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(directory)]
    return module


def _import_module(module_name: str, path: ModulePath, ref: str) -> Any:
    """스킬셋 루트를 기준으로 모듈을 격리 import 한다."""
    file = path.root / Path(*module_name.split(".")).with_suffix(".py")
    if not file.is_file():
        raise MalkuthError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"skill module not found: {module_name}",
            details={"skillset": ref, "expected_path": str(file)},
        )

    # 스킬셋 위치별 고유 이름으로 등록해 모듈 네임스페이스가 겹치지 않게 한다.
    # 같은 name@version 이라도 해석 루트가 다르면 다른 모듈이므로 경로를 키에 포함한다
    location = hashlib.sha256(str(path.root.resolve()).encode()).hexdigest()[:12]
    prefix = f"_malkuth_skillset_{path.name}_{location}"
    qualified = f"{prefix}.{module_name}"
    if qualified in sys.modules:
        return sys.modules[qualified]

    # 중간 패키지를 먼저 등록한다 — 없으면 스킬 코드의 `from .util import ...` 같은
    # 정상적인 스킬셋 내부 import 가 전부 실패한다
    _register_packages(prefix, module_name, path)

    spec = importlib.util.spec_from_file_location(qualified, file)
    if spec is None or spec.loader is None:
        raise MalkuthError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"failed to load skill module: {module_name}",
            details={"skillset": ref, "path": str(file)},
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except Exception as err:
        del sys.modules[qualified]
        raise MalkuthError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"skill module import failed: {module_name}",
            details={"skillset": ref, "path": str(file)},
        ) from err
    return module
