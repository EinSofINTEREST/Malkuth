"""Revoking from the permissions tab under a running agent (#283).

REST 흐름은 다른 E2E 가 본다. 여기서는 운영자가 **화면에서 클릭**한 회수가 떠 있는 에이전트의
다음 메모리 요청을 막고, 되돌리기 클릭이 재배포 없이 다시 연다는 것을 본다. 판정은 레지스트리 모드
Memory Service 가 요청마다 한다.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import CONTROL_TOKEN, api, plane_url, until

# 레지스트리를 켠 control plane 과 레지스트리 모드 Memory Service — 메모리 E2E 와 같은 구성
from tests.e2e.test_memory_access import LONGTERM, call, plane, started_at  # noqa: F401
from tests.e2e.test_stack import requires_docker
from tests.e2e.test_ui_browser import UI_TIMEOUT_MS, browser  # noqa: F401 — fixture

pytestmark = [pytest.mark.e2e, requires_docker]


@pytest.fixture
def page(browser, plane):  # noqa: F811, ARG001
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(UI_TIMEOUT_MS)
    failures: list[str] = []
    page.on("pageerror", lambda err: failures.append(str(err)))
    page.goto(f"{plane_url()}/ui/")
    page.fill("#token", CONTROL_TOKEN)
    page.click("#auth button[type=submit]")
    page.wait_for_function("() => document.querySelector('#health').textContent === '연결됨'")
    page.wait_for_function("() => document.querySelectorAll('#access-agent option').length > 0")
    yield page
    context.close()
    assert not failures, failures


def declared_row(page, target: str):
    return page.locator("#access-declared tbody tr", has_text=target).first


def test_a_revocation_clicked_in_the_permissions_tab_refuses_the_next_request(page):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    started = started_at()
    assert call("append", "longterm", LONGTERM) == 200, "선언된 local space 에 쓰지 못했다"

    # --- 권한 탭: 선언 권한이 보이고, 그 줄에서 쓰기를 회수한다
    page.click("#tabs button[data-tab=access]")
    page.select_option("#access-agent", "researcher")
    row = declared_row(page, LONGTERM)
    row.wait_for()
    assert "rw" in row.inner_text()
    page.fill("#access-reason", "e2e: 화면에서 회수")
    row.get_by_text("쓰기 회수").click()
    rule = page.locator("#access-rules tbody tr", has_text=LONGTERM).first
    rule.wait_for()
    assert "operator" in rule.inner_text() and "active" in rule.inner_text()

    # --- 떠 있는 에이전트의 다음 쓰기가 거부되고, 읽기는 남는다
    until(lambda: call("append", "longterm", LONGTERM) == 401, what="write refused", timeout_s=30)
    assert call("read", "longterm", LONGTERM) == 200, "쓰기 회수가 읽기까지 막았다"
    # 거부 판정이 화면에 남는다
    page.click("#access-form button[type=submit]")
    page.locator("#access-denials tbody tr", has_text=LONGTERM).first.wait_for()

    # --- 되돌리기 클릭: 재배포 없이 다시 쓴다
    page.locator("#access-rules tbody tr", has_text=LONGTERM).first.get_by_text("되돌리기").click()
    page.locator("#access-rules tbody tr", has_text=LONGTERM).first.locator(
        ".rule-lifted"
    ).wait_for()
    until(lambda: call("append", "longterm", LONGTERM) == 200, what="write restored", timeout_s=30)

    assert started_at() == started, "권한 변경이 컨테이너를 재시작했다"
