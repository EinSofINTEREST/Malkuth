"""The fake provider answers in the shape the contract asks for.

대역이 모든 출력 키를 문자열로 채우면, state schema 가 리스트로 선언한 키에
**선언과 다른 타입이 들어간다** — promptset 의 `{type: array}` 검증에 걸리거나
(그 경로가 있으면), 걸리지 않으면 조용히 계약이 깨진다 (#201).

E2E 로만 드러나던 것을 여기서 고정한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "deployments/docker/fake-provider"))


def answered(prompt: str) -> dict:
    """대역이 이 프롬프트에 돌려줄 JSON."""
    import server as fake

    return json.loads(fake._content_for(prompt, "digest"))


def asking(*keys: str) -> str:
    """템플릿이 계약을 적는 방식 그대로."""
    return f"Respond with a JSON object containing exactly these keys: {', '.join(keys)}"


@pytest.mark.parametrize("key", ["findings", "new_items", "seen_ids", "spaces", "pending_spaces"])
def test_a_list_key_comes_back_as_a_list(key: str):
    """state schema 가 리스트로 선언한 키 — 대역도 그 계약을 지켜야 한다."""
    assert isinstance(answered(asking(key))[key], list)


@pytest.mark.parametrize("key", ["plan", "report", "notified", "compacted"])
def test_a_scalar_key_stays_scalar(key: str):
    """리스트 처리가 나머지를 끌고 가면 반대 방향으로 틀린다."""
    assert isinstance(answered(asking(key))[key], str)


def test_every_asked_key_is_answered():
    """빠진 키는 `LLM_004` 로 태스크를 죽인다 — 대역이 계약을 다 채워야 한다."""
    keys = ("new_items", "seen_ids")

    assert set(answered(asking(*keys))) == set(keys)


def test_a_prompt_without_a_contract_gets_plain_text():
    """계약을 요구하지 않는 프롬프트까지 JSON 으로 답하면 형태가 어긋난다."""
    import server as fake

    assert not fake._content_for("just answer", "digest").startswith("{")
