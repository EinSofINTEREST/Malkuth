"""Every compose file must parse as a valid project.

`make up` 이 오래 깨져 있었다 (#226): compose 스키마가 조여지면서 개발 스택이
invalid 로 거절됐는데, 그 스택을 띄우는 경로가 CI 에도 테스트에도 없었다.
E2E 는 `compose.e2e.yaml` 만 쓰므로 개발 compose 의 파손을 잡지 못한다.

선언 자체는 unit (`tests/unit/test_compose.py`) 이 본다 — 여기서는 **compose 가
실제로 받아들이는지**를 본다. 그 판정은 compose 버전이 쥐고 있어 파싱으로는 흉내낼 수 없다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.integration.runtime.test_docker_lifecycle import requires_docker

DOCKER_DIR = Path(__file__).resolve().parents[3] / "deployments" / "docker"
COMPOSE_FILES = sorted(DOCKER_DIR.glob("compose*.yaml"))

pytestmark = [pytest.mark.integration, requires_docker]


def test_compose_files_are_discovered():
    """glob 이 비면 아래 테스트가 공허하게 통과한다."""
    assert COMPOSE_FILES, "compose 파일을 하나도 찾지 못했다"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_the_project_is_valid(path: Path):
    """`docker compose config` 가 거절하면 그 스택은 뜨지 않는다."""
    result = subprocess.run(  # noqa: S603 — 고정 인자, 셸 없음
        ["docker", "compose", "-f", str(path), "config"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{path.name} 이 invalid 다:\n{result.stderr}"
