"""Graph state schema utilities and merge rules.

그래프 state 스키마 해석과 병합 규칙. state 는 워크플로 계약 데이터이며,
노드는 선언된 키만 병합할 수 있다 — 전체 덮어쓰기는 금지된다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.topology import NodeSpec, resolve_import_ref

_STATE_PREFIX = "state."
_OUTPUT_PREFIX = "output."


def _state_error(message: str, **details: Any) -> MalkuthError:
    """state 스키마 불일치/병합 실패를 ``GRAPH_003`` 으로 만든다."""
    return MalkuthError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_003,
        message=message,
        details=details,
    )


def resolve_state_schema(ref: str) -> type[BaseModel]:
    """Resolve a graph state schema reference to a pydantic model.

    그래프 state 스키마 참조를 pydantic 모델로 해석합니다.

    Args:
        ref: Importable reference such as ``malkuth.graphs.schemas:ResearchState``.

    Returns:
        The resolved state model class.

    Raises:
        MalkuthError: GRAPH/``GRAPH_001`` if the ref cannot be imported,
            GRAPH/``GRAPH_003`` if the target is not a pydantic model.
    """
    target = resolve_import_ref(ref)
    if not (isinstance(target, type) and issubclass(target, BaseModel)):
        raise _state_error(f"state schema is not a pydantic model: {ref}", ref=ref)
    return target


def state_fields(schema: type[BaseModel]) -> frozenset[str]:
    """List the declared field names of a state schema.

    state 스키마의 선언된 필드 이름을 반환합니다 — ``input_map`` 검증에 쓰입니다.
    """
    return frozenset(schema.model_fields)


def extract_input(node: NodeSpec, state: dict[str, Any]) -> dict[str, Any]:
    """Build a task input mapping from graph state.

    ``input_map`` 선언에 따라 state 에서 태스크 입력을 추출합니다.
    ``state.<field>`` 형식만 state 를 참조하며, 그 외 값은 리터럴로 취급합니다.

    Args:
        node: The node whose ``input_map`` drives extraction.
        state: Current graph state.

    Returns:
        The task input mapping.

    Raises:
        MalkuthError: GRAPH/``GRAPH_003`` if a referenced field is absent from state.
    """
    extracted: dict[str, Any] = {}
    for key, source in node.input_map.items():
        # state 참조는 문자열 `state.<path>` 뿐 — 숫자/불리언/리스트 등은 리터럴
        if not (isinstance(source, str) and source.startswith(_STATE_PREFIX)):
            extracted[key] = source
            continue
        path = source.removeprefix(_STATE_PREFIX)
        extracted[key] = _read_path(state, path, node_id=node.id, source=source)
    return extracted


def _read_path(state: dict[str, Any], path: str, *, node_id: str, source: str) -> Any:
    """점 표기 경로로 state 값을 읽는다."""
    current: Any = state
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        raise _state_error(
            f"state field not found: {source}",
            node_id=node_id,
        )
    return current


def merge_output(
    node: NodeSpec,
    output: dict[str, Any],
    *,
    schema: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """Project a task output onto declared state keys.

    ``output_map`` 에 선언된 키만 state 갱신 대상으로 투영합니다 —
    노드가 state 전체를 덮어쓰는 패턴을 구조적으로 차단합니다.

    Args:
        node: The node whose ``output_map`` drives the projection.
        output: The task result output.
        schema: Optional state schema used to reject undeclared target keys.

    Returns:
        The partial state update (only declared keys).

    Raises:
        MalkuthError: GRAPH/``GRAPH_003`` if a mapped source is absent from the
            output, or a target key is not declared in the state schema.
    """
    update: dict[str, Any] = {}
    known = state_fields(schema) if schema is not None else None

    for state_key, source in node.output_map.items():
        if known is not None and state_key not in known:
            raise _state_error(
                f"output_map targets unknown state field: {state_key}",
                node_id=node.id,
            )
        path = source.removeprefix(_OUTPUT_PREFIX) if source.startswith(_OUTPUT_PREFIX) else source
        update[state_key] = _read_path(output, path, node_id=node.id, source=source)

    return update


def validate_state(schema: type[BaseModel], state: dict[str, Any]) -> dict[str, Any]:
    """Validate a state mapping against the graph state schema.

    state 매핑을 그래프 state 스키마로 검증합니다.

    Args:
        schema: The graph state model.
        state: The state mapping to validate.

    Returns:
        The validated state as a plain mapping.

    Raises:
        MalkuthError: GRAPH/``GRAPH_003`` if the state does not satisfy the schema.
    """
    try:
        return schema.model_validate(state).model_dump()
    except ValidationError as err:
        raise _state_error(
            "state does not satisfy the graph schema", schema=schema.__name__
        ) from err
