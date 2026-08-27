"""Reference promptset output-contract tests.

선언(`output_keys`)과 프롬프트 문구가 어긋나면 실행 시점에 `LLM_004` 로만
드러난다 — 배포 전에 잡아야 한다 (#147).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTSETS = REPO_ROOT / "modules" / "promptsets"
GRAPHS = REPO_ROOT / "graphs"

_ASKS_FOR = re.compile(r"exactly these keys: (.+)")


def declared_templates() -> list[tuple[str, str, list[str], Path]]:
    """출력 키를 선언한 템플릿 전부 — (promptset, 템플릿, 키, 파일)."""
    found = []
    for spec_file in sorted(PROMPTSETS.glob("*/*/promptset.yaml")):
        spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))["spec"]
        for name, template in spec["templates"].items():
            keys = template.get("output_keys")
            if keys:
                found.append(
                    (spec_file.parts[-3], name, list(keys), spec_file.parent / template["file"])
                )
    return found


@pytest.mark.parametrize(
    ("promptset", "template", "keys", "path"),
    declared_templates(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_the_prompt_asks_for_exactly_what_it_declares(promptset, template, keys, path):
    """선언과 문구가 갈라지면 모델은 다른 것을 내고 태스크는 LLM_004 로 죽는다."""
    body = path.read_text(encoding="utf-8")
    asked = _ASKS_FOR.search(body)

    assert asked is not None, f"{promptset}/{template} 이 출력 형태를 요구하지 않는다"
    assert [k.strip() for k in asked.group(1).split(",")] == keys


def test_every_graph_output_map_is_backed_by_a_declaration():
    """그래프가 기대하는 키를 아무도 선언하지 않으면 GRAPH_003 으로 실패한다.

    #142 에서 실제로 그랬다 — E2E 를 돌리기 전까지 드러나지 않았다.
    """
    declared = {
        (promptset, template): set(keys) for promptset, template, keys, _ in declared_templates()
    }

    unmet = []
    for graph_file in sorted(GRAPHS.glob("*.yaml")):
        graph = yaml.safe_load(graph_file.read_text(encoding="utf-8"))
        for node in graph["spec"]["nodes"]:
            wanted = set(node.get("output_map") or {})
            if not wanted or not node.get("agent"):
                continue
            promptset = node["agent"].split("/")[-1].split("@")[0]
            have = declared.get((promptset, node["id"]), set())
            if not wanted <= have:
                unmet.append(f"{graph_file.name}:{node['id']} wants {sorted(wanted - have)}")

    assert not unmet, f"선언되지 않은 출력 키: {unmet}"
