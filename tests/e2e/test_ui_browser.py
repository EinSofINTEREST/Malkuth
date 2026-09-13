"""Driving the operator UI with a real browser (#252).

다른 E2E 는 화면이 **부르는 것과 같은 REST 흐름**을 확인한다. 여기서는 브라우저가
실제로 클릭한다 — 폼 하나가 잘못 배선돼 있어도 REST 흐름은 초록일 수 있기 때문이다.

브라우저는 컨테이너에서 돌 수도 있고 호스트에서 돌 수도 있다. 호스트에 chromium 의
시스템 라이브러리가 없는 환경(개발 머신)에서도 **건드리지 않고** 돌리기 위해, 로컬
기동이 안 되면 공식 Playwright 이미지로 넘어간다. 어느 쪽을 썼는지는 출력에 남긴다.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterator
from importlib.metadata import version

import pytest

from tests.e2e.conftest import (
    CONTROL_TOKEN,
    api,
    deployed_containers,
    plane_url,
    until,
)
from tests.e2e.test_stack import docker, requires_docker

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright is a dev extra")

pytestmark = [pytest.mark.e2e, requires_docker]

PLAYWRIGHT_VERSION = version("playwright")
PLAYWRIGHT_IMAGE = f"mcr.microsoft.com/playwright:v{PLAYWRIGHT_VERSION}-noble"
GRAPH_NAME = "ui-clicked"
UI_TIMEOUT_MS = 20_000
DEPLOY_TIMEOUT_MS = 90_000
"""배포는 `DeploymentManager.ready_timeout_s`(기본 60s) 까지 걸릴 수 있다."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def container_running(name: str) -> bool:
    return docker("inspect", "-f", "{{.State.Running}}", name, check=False).strip() == "true"


def start_browser_server(playwright, attempts: int = 3):
    """Start the browser server and connect, retrying on a fresh port.

    `--network host` 라 포트는 호스트의 것이다 — 고른 뒤 컨테이너가 잡기까지의 틈에
    다른 프로세스가 차지할 수 있다. 그때 TCP 는 열려 있으므로 "포트가 열렸다" 는
    준비 신호가 되지 못한다. **연결이 되는 것**을 준비 신호로 쓰고, 안 되면 컨테이너
    상태와 로그를 붙여 새 포트로 다시 시도한다.
    """
    failures = []
    for _ in range(attempts):
        port = free_port()
        name = f"malkuth-e2e-playwright-{os.getpid()}-{port}"
        docker("rm", "-f", name, check=False)
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "host",
            "--ipc=host",
            PLAYWRIGHT_IMAGE,
            "npx",
            "-y",
            f"playwright@{PLAYWRIGHT_VERSION}",
            "run-server",
            "--port",
            str(port),
            # host 네트워크를 공유하므로 여기서의 loopback 이 곧 호스트의 loopback 이다.
            # 0.0.0.0 으로 열면 **인증 없는 브라우저 제어 소켓**이 모든 인터페이스에 노출된다
            "--host",
            "127.0.0.1",
            timeout=900,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and container_running(name) and not port_open(port):
            time.sleep(1.0)
        try:
            if not container_running(name):
                raise AssertionError("browser server exited")
            return playwright.chromium.connect(f"ws://127.0.0.1:{port}/"), name
        except Exception as err:  # noqa: BLE001 — 다음 포트로 다시 시도한다
            failures.append(
                f"port {port}: {type(err).__name__} {err}\n{docker('logs', name, check=False)}"
            )
            docker("rm", "-f", name, check=False)
    raise AssertionError("browser server never became connectable:\n" + "\n".join(failures))


@pytest.fixture(scope="module")
def browser() -> Iterator:
    """A real browser — locally when the host can run one, in a container otherwise.

    컨테이너 경로는 `--network host` 다: 브라우저가 호스트 loopback 의 control plane 을
    봐야 하기 때문이다. 에이전트 컨테이너의 host 네트워크 금지(02 Network 5)는 에이전트에
    대한 규칙이고, 이것은 테스트 도구다.
    """
    with playwright_api.sync_playwright() as p:
        try:
            local = p.chromium.launch()
        except Exception as err:  # noqa: BLE001 — 왜 못 띄웠는지는 아래에서 보고한다
            print(f"local chromium unavailable ({type(err).__name__}) — using {PLAYWRIGHT_IMAGE}")
        else:
            print("using the host's chromium")
            try:
                yield local
            finally:
                local.close()
            return

        remote, name = start_browser_server(p)
        try:
            yield remote
        finally:
            remote.close()
            docker("rm", "-f", name, check=False)


@pytest.fixture
def page(browser, plane):  # noqa: ARG001 — control plane 이 먼저 떠 있어야 화면이 읽는다
    """연결까지 마친 화면 — 토큰을 넣고 **연결** 을 눌러 카탈로그가 로드된 상태."""
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(UI_TIMEOUT_MS)
    failures: list[str] = []
    page.on("pageerror", lambda err: failures.append(str(err)))
    # 삭제는 confirm() 을 띄운다 — 자동화에서는 받아들인다
    page.on("dialog", lambda dialog: dialog.accept())

    page.goto(f"{plane_url()}/ui/")
    page.fill("#token", CONTROL_TOKEN)
    page.click("#auth button[type=submit]")
    page.wait_for_function("() => document.querySelector('#health').textContent === '연결됨'")
    # `연결됨` 은 **연결**이 됐다는 뜻이고 목록은 그 뒤에 채워진다 — 화면이 쓸 수 있는
    # 상태가 되는 것을 기다린다. 이것을 건너뛰면 테스트가 빈 목록과 경쟁한다
    page.wait_for_function("() => document.querySelectorAll('#catalog-agents li').length > 0")

    yield page

    # 화면에서 난 예외는 조용히 지나가면 안 된다 — 클릭이 성공한 것처럼 보일 수 있다
    context.close()
    assert not failures, failures


def tab(page, name: str) -> None:
    page.click(f"#tabs button[data-tab={name}]")


def test_the_catalog_shows_what_the_repository_declares(page):
    """화면이 열리고 카탈로그가 채워진다 — 여기가 조립의 출발점이다."""
    tab(page, "catalog")

    agents = page.locator("#catalog-agents li")
    graphs = page.locator("#catalog-graphs li")

    assert agents.count() > 0
    assert any("planner" in agents.nth(i).inner_text() for i in range(agents.count()))
    assert any("research-pipeline" in graphs.nth(i).inner_text() for i in range(graphs.count()))
    assert page.locator("#catalog-problems li").count() == 0


def test_assembling_deploying_running_and_destroying_by_clicking(page):
    """#239 완료 조건 — 브라우저에서 클릭만으로 만들고, 띄우고, 돌리고, 내린다."""
    try:
        _compose_graph(page)
        _deploy(page)
        _run(page)
        _tear_down(page)
    finally:
        api("DELETE", f"/v1/graphs/{GRAPH_NAME}")


def _compose_graph(page) -> None:
    tab(page, "graph")
    page.fill("#graph-form [name=name]", GRAPH_NAME)
    page.fill("#graph-form [name=version]", "0.1.0")
    page.fill("#graph-form [name=description]", "clicked together in the ui")
    page.fill("#graph-form [name=goal]", "assemble a system by clicking")
    page.fill("#graph-form [name=state_schema]", "malkuth.graphs.schemas:ResearchState")

    # 노드 — 에이전트는 카탈로그에서 온 드롭다운이다
    page.click("[data-add=node]")
    row = page.locator("#graph-nodes tbody tr").first
    row.locator("[name=node_id]").fill("planner")
    row.locator("[name=node_agent]").select_option(label="planner@0.4.0")

    # 편집기가 템플릿의 필수 변수를 채워 준다 (#260) — 사람이 외우지 않는다
    page.wait_for_function(
        "() => document.querySelector('#graph-nodes tbody tr [name=node_input]').value"
        ".includes('query=state.query')"
    )
    row.locator("[name=node_output]").fill("plan=output.plan")

    # 엣지 — 첫 행은 START 로 미리 놓여 있다
    edges = page.locator("#graph-edges tbody tr")
    edges.first.locator("[name=edge_to]").fill("planner")
    page.click("[data-add=edge]")
    last = edges.last
    last.locator("[name=edge_from]").fill("planner")
    last.locator("[name=edge_to]").fill("END")

    page.click("#graph-validate")
    page.wait_for_selector("#graph-findings li.ok")
    assert page.locator("#graph-findings li.ok").inner_text() == "검증 통과"

    page.click("#graph-form button[type=submit]")
    page.wait_for_function("() => document.querySelector('#status').textContent.includes('저장됨')")

    tab(page, "catalog")
    page.wait_for_function(
        "(name) => [...document.querySelectorAll('#catalog-graphs li')]"
        ".some((li) => li.textContent.includes(name))",
        arg=GRAPH_NAME,
    )


def _deploy(page) -> None:
    tab(page, "deployments")
    page.select_option("#deploy-graph", GRAPH_NAME)
    page.click("#deploy-form button[type=submit]")

    row = page.locator("#deployments tbody tr", has_text=GRAPH_NAME).first
    # 배포는 에이전트가 healthy 가 될 때까지 기다린다 — manager 의 상한(기본 60s)보다
    # 짧게 잡으면 느린 기동에서 화면이 아니라 테스트가 먼저 포기한다
    row.locator("td.status-ready").wait_for(timeout=DEPLOY_TIMEOUT_MS)
    assert "malkuth-planner-0" in deployed_containers()


def _run(page) -> None:
    tab(page, "runs")
    page.select_option("#run-deployment", index=0)
    page.fill("#run-input", json.dumps({"query": "clicked"}))
    page.click("#run-form button[type=submit]")

    # 제출은 즉시 돌아오고 표가 완주까지 따라간다
    page.wait_for_selector("#runs tbody tr td.status-completed", timeout=UI_TIMEOUT_MS * 3)
    page.locator("#runs tbody tr").first.get_by_text("보기").click()
    page.wait_for_function(
        "() => document.querySelector('#run-detail').textContent.includes('plan')"
    )


def _tear_down(page) -> None:
    tab(page, "deployments")
    page.locator("#deployments tbody tr", has_text=GRAPH_NAME).first.get_by_text("해체").click()
    page.wait_for_selector(
        f"#deployments tbody tr:has-text('{GRAPH_NAME}') td.status-stopped",
        timeout=UI_TIMEOUT_MS * 2,
    )
    until(lambda: not deployed_containers(), what="containers removed", timeout_s=60)

    tab(page, "graph")
    page.click("#graph-delete")  # confirm() 은 위의 dialog 핸들러가 받는다
    page.wait_for_function("() => document.querySelector('#status').textContent.includes('삭제됨')")
