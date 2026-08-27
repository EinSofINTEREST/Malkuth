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
AGENTS = REPO_ROOT / "agents"

_ASKS_FOR = re.compile(r"exactly these keys: (.+)")


def declared_templates() -> list[tuple[str, str, list[str], Path]]:
    """출력 키를 선언한 템플릿 전부 — (``name@version``, 템플릿, 키, 파일).

    **버전까지 키에 넣는다**: 버리면 0.1.0 을 검증하고 런타임은 0.2.0 을
    로드하는 어긋남을 놓친다.
    """
    found = []
    for spec_file in sorted(PROMPTSETS.glob("*/*/promptset.yaml")):
        spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))["spec"]
        ref = f"{spec_file.parts[-3]}@{spec_file.parts[-2]}"
        for name, template in spec["templates"].items():
            keys = template.get("output_keys")
            if keys:
                found.append((ref, name, list(keys), spec_file.parent / template["file"]))
    return found


def agent_promptset_refs() -> dict[str, str]:
    """에이전트 ``name@version`` → 그 manifest 가 쓰는 promptset ``name@version``."""
    refs = {}
    for manifest_file in sorted(AGENTS.glob("*/manifest.yaml")):
        declared = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
        meta, spec = declared["metadata"], declared["spec"]
        refs[f"{meta['name']}@{meta['version']}"] = spec["promptset"]["ref"].split("/", 1)[1]
    return refs


@pytest.mark.parametrize(
    ("promptset_ref", "template", "keys", "path"),
    declared_templates(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_the_prompt_asks_for_exactly_what_it_declares(promptset_ref, template, keys, path):
    """선언과 문구가 갈라지면 모델은 다른 것을 내고 태스크는 LLM_004 로 죽는다."""
    body = path.read_text(encoding="utf-8")
    asked = _ASKS_FOR.search(body)

    assert asked is not None, f"{promptset_ref}/{template} 이 출력 형태를 요구하지 않는다"
    assert [k.strip() for k in asked.group(1).split(",")] == keys


def test_every_graph_output_map_is_backed_by_a_declaration():
    """그래프가 읽는 출력 경로를 아무도 선언하지 않으면 GRAPH_003 으로 실패한다.

    #142 에서 실제로 그랬다 — E2E 를 돌리기 전까지 드러나지 않았다.

    **매핑의 값(`output.plan`)을 본다**: 키(`plan`)만 보면
    ``plan: output.missing`` 같은 매핑이 통과하고 런타임에 실패한다.
    그리고 **그래프가 고른 에이전트 버전**을 따라가야 한다 — 버전을 버리면
    한 버전을 검증하고 다른 버전이 로드된다.
    """
    declared = {(ref, template): set(keys) for ref, template, keys, _ in declared_templates()}
    promptset_of = agent_promptset_refs()

    unmet = []
    for graph_file in sorted(GRAPHS.glob("*.yaml")):
        graph = yaml.safe_load(graph_file.read_text(encoding="utf-8"))
        for node in graph["spec"]["nodes"]:
            mapping = node.get("output_map") or {}
            if not mapping or not node.get("agent"):
                continue

            # output_map 의 **값**이 실행기 출력에서 읽는 경로다
            wanted = {
                str(source).removeprefix("output.")
                for source in mapping.values()
                if str(source).startswith("output.")
            }
            agent_ref = node["agent"].split("/", 1)[1]
            promptset = promptset_of.get(agent_ref)
            if promptset is None:
                unmet.append(f"{graph_file.name}:{node['id']} unknown agent {agent_ref}")
                continue

            have = declared.get((promptset, node["id"]), set())
            if not wanted <= have:
                unmet.append(
                    f"{graph_file.name}:{node['id']} ({promptset}) wants {sorted(wanted - have)}"
                )

    assert not unmet, f"선언되지 않은 출력 경로: {unmet}"
