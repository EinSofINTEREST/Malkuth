"""Declared memory permissions (#278)."""

from __future__ import annotations

from pathlib import Path

import pytest

from malkuth.access.baselines import MemoryBaseline
from malkuth.access.model import Mode
from malkuth.catalog import Catalog
from tests.fixtures.access import agent, group, write


def with_spaces(document: dict, *aliases: str) -> dict:
    document["spec"]["memory"] = {
        "spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": a} for a in aliases]
    }
    return document


@pytest.fixture
def baseline(tmp_path: Path) -> MemoryBaseline:
    write(
        tmp_path / "agents" / "worker" / "manifest.yaml",
        with_spaces(agent("worker", "research"), "longterm"),
    )
    write(tmp_path / "agents" / "outsider" / "manifest.yaml", agent("outsider"))
    write(tmp_path / "agents" / "librarian" / "manifest.yaml", agent("librarian"))
    write(
        tmp_path / "groups" / "research.yaml",
        group(
            "research",
            {
                "memory": {
                    "spaces": [
                        {
                            "ref": "memorysets/domain-knowledge@0.1.0",
                            "as": "knowledge",
                            "mode": "rw",
                        },
                        {"ref": "memorysets/domain-knowledge@0.1.0", "as": "archive", "mode": "ro"},
                    ]
                }
            },
        ),
    )
    write(
        tmp_path / "groups" / "global.yaml",
        group(
            "global",
            {
                "memory": {
                    "spaces": [
                        {"ref": "memorysets/org-facts@0.1.0", "as": "org", "writers": ["librarian"]}
                    ]
                }
            },
        ),
    )
    return MemoryBaseline(Catalog.under(tmp_path))


@pytest.mark.parametrize(
    ("who", "target", "mode", "allowed", "why"),
    [
        ("worker", "local:worker:longterm", Mode.RW, True, "자기 local space"),
        ("worker", "local:worker:diary", Mode.RO, False, "선언하지 않은 local space"),
        ("outsider", "local:worker:longterm", Mode.RO, False, "남의 local space"),
        ("worker", "group:research:knowledge", Mode.RW, True, "그룹 rw space"),
        ("worker", "group:research:archive", Mode.RO, True, "그룹 ro space 읽기"),
        ("worker", "group:research:archive", Mode.RW, False, "그룹 ro space 쓰기"),
        ("outsider", "group:research:knowledge", Mode.RO, False, "비멤버는 읽기도 못 한다"),
        ("outsider", "global:global:org", Mode.RO, True, "전역은 모두 읽는다"),
        ("outsider", "global:global:org", Mode.RW, False, "writers 밖의 전역 쓰기"),
        ("librarian", "global:global:org", Mode.RW, True, "writers 의 전역 쓰기"),
        ("worker", "global:research:org", Mode.RO, False, "global 이 아닌 소유자"),
        ("worker", "run:r-1:scratch", Mode.RO, False, "run space 는 신원만으로 판정하지 않는다"),
        ("worker", "longterm", Mode.RO, False, "별칭은 대상이 아니다"),
        ("ghost", "global:global:org", Mode.RO, False, "없는 에이전트"),
    ],
)
def test_declarations_decide_memory(baseline, who, target, mode, allowed, why):
    assert baseline.allows(who, target, mode) is allowed, why


def test_a_declaration_change_is_read_on_the_next_decision(baseline, tmp_path):
    """재시작 없이 — 그룹 space 를 ro 로 강등하면 다음 판정부터 쓰기가 없다."""
    assert baseline.allows("worker", "group:research:knowledge", Mode.RW)

    write(
        tmp_path / "groups" / "research.yaml",
        group(
            "research",
            {
                "memory": {
                    "spaces": [
                        {
                            "ref": "memorysets/domain-knowledge@0.1.0",
                            "as": "knowledge",
                            "mode": "ro",
                        }
                    ]
                }
            },
        ),
    )

    assert not baseline.allows("worker", "group:research:knowledge", Mode.RW)
    assert baseline.allows("worker", "group:research:knowledge", Mode.RO)
