"""Hot reload actually reloads (#274).

02 Hot Reload 는 ``POST /v1/reload`` 로 promptset/skillset 무중단 리로드를 약속한다. 엔드포인트는
``reloaded`` 로 답했지만 진입점이 훅을 넘기지 않아 아무것도 다시 읽지 않았다.

세 층을 따로 본다:
- 실행기: 교체는 **새 태스크부터** — 진행 중 태스크는 시작할 때 잡은 묶음으로 끝난다
- 조립: 훅이 실제 모듈 파일을 다시 읽고, 실패하면 이전 묶음을 그대로 둔다
- 진입점: 떠 있는 앱의 엔드포인트가 그 훅에 닿는다
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from malkuth.agentd import __main__ as agentd
from malkuth.agentd.executor import Executor, ModuleBinding
from malkuth.core.errors import MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.observability.metrics import Metrics
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text
from tests.fixtures.waiting import until

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- 실행기: 새 태스크부터 -------------------------------------------------------


def binding(label: str, tools: FakeTools) -> ModuleBinding:
    return ModuleBinding(
        tools=tools,
        render=lambda task: f"{label} prompt",
        tool_schemas=(f"{label}-tool",),
    )


async def test_a_task_in_flight_finishes_on_the_binding_it_started_with():
    """도중에 갈아 끼우면 한 태스크 안에서 옛 프롬프트와 새 도구 목록이 섞인다."""
    old_tools = FakeTools().script("slow", delay=0.05)
    model = FakeModel([calls("slow"), text("done")])
    first = binding("old", old_tools)
    executor = Executor(
        agent="a",
        model=model,
        tools=first.tools,
        render=first.render,
        tool_schemas=first.tool_schemas,
    )

    running = asyncio.create_task(executor.execute(make_task(task_id="in-flight")))
    await until(lambda: "slow" in old_tools.started)
    executor.rebind(binding("new", FakeTools()))
    await running

    prompts_and_tools = [(prompt.split()[0], tools) for prompt, tools in model.calls]
    assert prompts_and_tools == [("old", ("old-tool",)), ("old", ("old-tool",))]


async def test_a_task_started_after_the_swap_uses_the_new_binding():
    model = FakeModel([text("done")])
    first = binding("old", FakeTools())
    executor = Executor(
        agent="a",
        model=model,
        tools=first.tools,
        render=first.render,
        tool_schemas=first.tool_schemas,
    )

    executor.rebind(binding("new", FakeTools()))
    await executor.execute(make_task(task_id="after"))

    assert model.calls == [("new prompt", ("new-tool",))]


# --- 조립: 실제 모듈 파일을 다시 읽는다 -----------------------------------------------


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """레포 모듈의 사본 — 템플릿 파일을 고쳐도 레포는 그대로다."""
    copy = tmp_path / "app"
    shutil.copytree(
        REPO_ROOT / "modules", copy / "modules", ignore=shutil.ignore_patterns("__pycache__")
    )
    monkeypatch.setenv("MALKUTH_ROOT", str(copy))
    monkeypatch.delenv(agentd.EXECUTOR_ENV, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    return copy


def planner() -> AgentManifest:
    doc = yaml.safe_load((REPO_ROOT / "agents" / "planner" / "manifest.yaml").read_text("utf-8"))
    doc["spec"].pop("memory", None)
    doc["spec"]["a2a"] = {"enabled": False}
    return AgentManifest.model_validate(doc)


DIRECT = {"node_id": None, "input": {"query": "q"}}


def declare_fresh_template(root: Path) -> None:
    """promptset 선언에 새 템플릿을 더한다.

    템플릿 **본문**은 렌더할 때마다 디스크에서 읽으므로 리로드 없이도 바뀐다. 기동 시 한 번만
    읽히는 것은 선언(템플릿 목록·변수·출력 키)과 스킬셋 코드다 — 리로드가 증명해야 하는 것은
    이쪽이다.
    """
    promptset = root / "modules" / "promptsets" / "planner" / "0.3.0"
    (promptset / "templates" / "fresh.j2").write_text("FRESH {{ query }}\n", encoding="utf-8")
    document = yaml.safe_load((promptset / "promptset.yaml").read_text(encoding="utf-8"))
    document["spec"]["templates"]["fresh"] = {
        "file": "templates/fresh.j2",
        "variables": {"query": {"type": "string", "required": True}},
    }
    (promptset / "promptset.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


FRESH = {"node_id": "fresh", "input": {"query": "q"}}


async def test_reload_picks_up_a_changed_declaration(root):
    manifest = planner()
    executor = await agentd.build_executor(manifest)
    reload = agentd.build_reload(manifest, executor)
    declare_fresh_template(root)

    with pytest.raises(MalkuthError):
        executor.binding.render(make_task(**FRESH))  # 리로드 전: 기동 때의 선언에 없다

    card = await reload()

    assert executor.binding.render(make_task(**FRESH)).startswith("FRESH q")
    assert card["name"] == "planner"


async def test_a_failed_reload_keeps_the_previous_binding(root):
    """반쯤 로드된 도구 목록으로 태스크를 받지 않는다 — 조립이 끝나야 교체한다."""
    manifest = planner()
    executor = await agentd.build_executor(manifest)
    reload = agentd.build_reload(manifest, executor)
    before = executor.binding
    (root / "modules" / "promptsets" / "planner" / "0.3.0" / "promptset.yaml").unlink()

    with pytest.raises(MalkuthError):
        await reload()

    assert executor.binding is before


async def test_reload_keeps_the_wiring_it_did_not_load(monkeypatch):
    """메모리·peer·MCP 는 모듈이 아니라 배선이다 — 리로드가 새로 만들면 토큰과 세션이 바뀐다."""
    memory, peers, mcp = object(), object(), None
    tools = agentd.AgentToolRegistry(agent="planner", memory=memory, peers=peers, mcp=mcp)
    executor = Executor(
        agent="planner", model=FakeModel([text("x")]), tools=tools, render=lambda t: ""
    )

    captured = {}

    async def fake_load(manifest, registry, **wiring):
        captured.update(wiring)
        return ModuleBinding(tools=tools, render=lambda t: "")

    monkeypatch.setattr(agentd, "load_modules", fake_load)
    await agentd.build_reload(planner(), executor)()

    assert captured["memory"] is memory
    assert captured["peers"] is peers


# --- 진입점: 떠 있는 앱의 엔드포인트가 훅에 닿는다 ---------------------------------------


def test_the_served_app_reloads_the_standard_executor(root, tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(planner().model_dump(mode="json", by_alias=True, exclude_none=True)),
        encoding="utf-8",
    )
    monkeypatch.setenv(agentd.MANIFEST_ENV, str(manifest_path))
    monkeypatch.setenv(agentd.TOKEN_ENV, "agent-token")
    monkeypatch.setattr(agentd, "_setup_observability", Metrics)
    served = {}
    monkeypatch.setattr(
        agentd, "_serve", lambda app, manifest, executor: served.update(app=app, executor=executor)
    )

    agentd.main()

    declare_fresh_template(root)
    response = TestClient(served["app"]).post(
        "/v1/reload", headers={"Authorization": "Bearer agent-token"}
    )

    assert response.json()["status"] == "reloaded"
    assert served["executor"].binding.render(make_task(**FRESH)).startswith("FRESH q")
