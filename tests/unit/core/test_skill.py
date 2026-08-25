"""Unit tests for the skill decorator and schema derivation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel

from malkuth.core.skill import SkillContext, build_spec, get_spec, skill


@skill
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색 질의
        max_results: 최대 결과 개수
    """
    return []


def test_skill_attaches_spec():
    spec = get_spec(search)

    assert spec is not None
    assert spec.name == "search"


def test_schema_snapshot():
    """시그니처 → tool schema 계약 고정.

    이 스냅샷이 깨지면 모델이 보는 계약이 바뀐 것이므로 의도한 변경인지 확인해야 한다.
    """
    spec = get_spec(search)
    assert spec is not None

    assert spec.model_dump() == {
        "name": "search",
        "description": "웹 검색을 수행하고 상위 결과를 반환합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    }


def test_context_parameter_is_excluded_from_schema():
    """SkillContext 는 프레임워크가 주입한다 — 모델이 채우는 인자가 아니다."""
    spec = get_spec(search)
    assert spec is not None

    assert "ctx" not in spec.parameters["properties"]


def test_tool_schema_shape():
    spec = get_spec(search)
    assert spec is not None

    schema = spec.to_tool_schema()

    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["input_schema"] == spec.parameters


def test_name_override():
    @skill(name="fetch_page")
    async def fetch(ctx: SkillContext, url: str) -> str:
        """URL 의 본문 텍스트를 추출합니다."""
        return ""

    spec = get_spec(fetch)
    assert spec is not None
    assert spec.name == "fetch_page"


def test_sync_function_is_rejected():
    """모든 skill 은 async — 04-module-system.md Skill Implementation Rules 2."""

    def sync_skill(ctx: SkillContext, q: str) -> str:
        """동기 함수."""
        return q

    with pytest.raises(ValueError, match="must be an async function"):
        build_spec(sync_skill)


def test_function_without_context_is_rejected():
    async def no_ctx() -> str:
        """컨텍스트 없음."""
        return ""

    with pytest.raises(ValueError, match="SkillContext as first parameter"):
        build_spec(no_ctx)


async def _typed(
    ctx: SkillContext,
    text: str,
    count: int,
    ratio: float,
    flag: bool,
    items: list[str],
    mapping: dict[str, int],
    optional: str | None = None,
) -> None:
    """다양한 타입."""


def test_type_hint_mapping():
    spec = build_spec(_typed)
    props = spec.parameters["properties"]

    assert props["text"] == {"type": "string"}
    assert props["count"] == {"type": "integer"}
    assert props["ratio"] == {"type": "number"}
    assert props["flag"] == {"type": "boolean"}
    assert props["items"] == {"type": "array", "items": {"type": "string"}}
    assert props["mapping"] == {"type": "object"}
    assert props["optional"] == {"type": "string", "default": None}
    assert spec.parameters["required"] == [
        "text",
        "count",
        "ratio",
        "flag",
        "items",
        "mapping",
    ]


def test_description_stops_at_args_section():
    async def documented(ctx: SkillContext, q: str) -> str:
        """첫 줄 설명.

        Args:
            q: 질의
        """
        return q

    spec = build_spec(documented)

    assert spec.description == "첫 줄 설명."


def test_missing_docstring_yields_empty_description():
    async def undocumented(ctx: SkillContext) -> None:
        return None

    assert build_spec(undocumented).description == ""


def test_get_spec_returns_none_for_plain_function():
    async def plain(ctx: SkillContext) -> None:
        """평범한 함수."""

    assert get_spec(plain) is None


async def test_decorated_skill_remains_callable():
    ctx = SkillContext(agent="a", task_id="t", run_id="r")

    assert await search(ctx, "query") == []


class Priority(StrEnum):
    """우선순위."""

    LOW = "low"
    HIGH = "high"


class Payload(BaseModel):
    """구조화 입력."""

    title: str


async def _advanced(
    ctx: SkillContext,
    priority: Priority,
    payload: Payload,
    mixed: str | int,
    raw: Any,
    untyped=None,
) -> None:
    """고급 타입."""


def test_str_enum_becomes_enum_schema():
    props = build_spec(_advanced).parameters["properties"]

    assert props["priority"] == {"type": "string", "enum": ["low", "high"]}


def test_pydantic_model_becomes_json_schema():
    props = build_spec(_advanced).parameters["properties"]

    assert props["payload"]["type"] == "object"
    assert "title" in props["payload"]["properties"]


def test_multi_type_union_becomes_any_of():
    props = build_spec(_advanced).parameters["properties"]

    assert props["mixed"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_any_and_untyped_params_have_no_constraint():
    props = build_spec(_advanced).parameters["properties"]

    assert props["raw"] == {}
    assert props["untyped"] == {"default": None}


async def _variadic(ctx: SkillContext, first: str, *args: str, **kwargs: str) -> None:
    """가변 인자."""


def test_variadic_params_are_excluded():
    props = build_spec(_variadic).parameters["properties"]

    assert set(props) == {"first"}
