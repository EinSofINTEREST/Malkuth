"""Skill decorator and context.

``@skill`` 데코레이터는 함수 시그니처 + type hint 로부터 tool JSON schema 를
자동 생성한다 — 수기 JSON schema 작성을 금지하기 위한 장치다.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from malkuth.core.agent import SecretsProvider

SKILL_ATTR = "__malkuth_skill__"


@runtime_checkable
class ArtifactStore(Protocol):
    """Artifact storage access.

    대용량 산출물 저장소 — output 에 직접 싣지 않고 참조로 전달하기 위한 계약.
    """

    async def put(self, key: str, data: bytes) -> str:
        """산출물을 저장하고 참조를 반환한다."""
        ...

    async def get(self, ref: str) -> bytes:
        """참조로부터 산출물을 읽는다."""
        ...


@dataclass(frozen=True)
class SkillContext:
    """Runtime context handed to every skill invocation.

    Skill 실행 컨텍스트. 로거/secrets/artifact 접근은 반드시 이 컨텍스트를
    통해서만 — 전역 상태나 모듈 레벨 클라이언트 초기화는 금지된다.
    """

    agent: str
    task_id: str
    run_id: str
    logger: Any | None = None
    secrets: SecretsProvider | None = None
    artifacts: ArtifactStore | None = None


class SkillSpec(BaseModel):
    """The tool contract derived from a skill function.

    Skill 함수로부터 도출된 tool 계약 — 모델이 보는 스키마 그 자체다.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]

    def to_tool_schema(self) -> dict[str, Any]:
        """Render the schema a model provider consumes.

        모델 provider 가 소비하는 tool 스키마 표현으로 변환합니다.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


_PRIMITIVE_SCHEMAS: dict[type, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """타입 힌트를 JSON schema 조각으로 변환한다."""
    if annotation is Any or annotation is inspect.Parameter.empty:
        return {}

    if annotation in _PRIMITIVE_SCHEMAS:
        return dict(_PRIMITIVE_SCHEMAS[annotation])

    origin = get_origin(annotation)

    if origin in (Union, types.UnionType):
        # None 을 버리면 str | None 이 string 전용 스키마가 되어, 모델이 null 을
        # 보낼 수 없는데 default 는 None 인 모순된 계약이 만들어진다
        schemas = [
            {"type": "null"} if arg is type(None) else _schema_for_annotation(arg)
            for arg in get_args(annotation)
        ]
        if not schemas:
            return {}
        if len(schemas) == 1:
            return schemas[0]
        return {"anyOf": schemas}

    if origin in (list, Sequence, tuple):
        args = list(get_args(annotation))
        items = _schema_for_annotation(args[0]) if args else {}
        return {"type": "array", "items": items}

    if origin is dict:
        dict_args = get_args(annotation)
        if len(dict_args) == 2:
            return {
                "type": "object",
                "additionalProperties": _schema_for_annotation(dict_args[1]),
            }
        return {"type": "object"}

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.model_json_schema()

    if isinstance(annotation, type) and issubclass(annotation, str):
        # StrEnum 등 str 서브클래스
        values = getattr(annotation, "__members__", None)
        if values:
            return {"type": "string", "enum": [str(v.value) for v in values.values()]}
        return {"type": "string"}

    return {}


def _description_from_docstring(fn: Callable[..., Any]) -> str:
    """docstring 첫 문단을 tool description 으로 쓴다."""
    doc = inspect.getdoc(fn)
    if not doc:
        return ""
    lines: list[str] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped:
            break
        if stripped.rstrip(":") in {"Args", "Returns", "Raises"}:
            break
        lines.append(stripped)
    return " ".join(lines)


def build_spec(fn: Callable[..., Any], *, name: str | None = None) -> SkillSpec:
    """Derive a tool contract from a skill function.

    Skill 함수 시그니처와 docstring 으로부터 tool 계약을 도출합니다.
    첫 파라미터인 ``SkillContext`` 는 스키마에서 제외됩니다 — 프레임워크가
    주입하는 값이지 모델이 채우는 인자가 아니기 때문입니다.

    Args:
        fn: The skill function (must be ``async def``).
        name: Optional tool name override; defaults to the function name.

    Returns:
        The derived skill specification.

    Raises:
        ValueError: If the function is not async, has no parameters, or its first
            parameter is annotated as something other than :class:`SkillContext`.
    """
    if not inspect.iscoroutinefunction(fn):
        raise ValueError(f"skill '{fn.__name__}' must be an async function")

    signature = inspect.signature(fn)
    params = list(signature.parameters.values())
    if not params:
        raise ValueError(f"skill '{fn.__name__}' must accept a SkillContext as first parameter")

    try:
        hints = get_type_hints(fn)
    except Exception:  # noqa: BLE001 - 해석 불가한 힌트는 스키마 없이 진행
        hints = {}

    # 첫 파라미터는 반드시 SkillContext — 어긋나면 런타임 주입 계약이 조용히 깨진다.
    # 힌트가 아예 없는 경우는 허용한다 (동적 정의 skill 을 막지 않기 위해)
    context_hint = hints.get(params[0].name)
    if context_hint is not None and context_hint is not SkillContext:
        raise ValueError(
            f"skill '{fn.__name__}' first parameter must be SkillContext, "
            f"got {getattr(context_hint, '__name__', context_hint)}"
        )

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in params[1:]:
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(param.name, param.annotation)
        schema = _schema_for_annotation(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
        else:
            schema = {**schema, "default": param.default}
        properties[param.name] = schema

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }

    return SkillSpec(
        name=name or fn.__name__,
        description=_description_from_docstring(fn),
        parameters=parameters,
    )


def skill(fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Callable[..., Any]:
    """Mark an async function as a skill and attach its derived tool schema.

    Async 함수를 skill 로 표시하고 도출된 tool 스키마를 부착합니다.
    시그니처가 곧 스키마이므로 수기 JSON schema 작성은 필요하지 않습니다.

    Args:
        fn: The skill function when used without parentheses.
        name: Optional tool name override.

    Returns:
        The same function, annotated with its :class:`SkillSpec`.
    """

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        spec = build_spec(target, name=name)
        setattr(target, SKILL_ATTR, spec)
        return target

    if fn is not None:
        return decorate(fn)
    return decorate


def get_spec(fn: Callable[..., Any]) -> SkillSpec | None:
    """Read the skill spec attached by :func:`skill`.

    ``@skill`` 이 부착한 스펙을 읽습니다. skill 이 아니면 ``None``.
    """
    spec = getattr(fn, SKILL_ATTR, None)
    return spec if isinstance(spec, SkillSpec) else None
