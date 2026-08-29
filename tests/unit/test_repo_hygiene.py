"""No test artifact should be tracked in the repository.

테스트가 저장소 루트에 파일을 남기고 그것이 커밋에 실린 적이 있다 —
` :memory: ` (앞뒤 공백) 는 sqlite 의 특수 식별자가 아니라 문자 그대로의 경로라
실제 파일이 생겼다. 리뷰가 잡아 주었지만, 자동으로 잡는 것이 없었다.

여기서는 **추적 중인 파일 목록**을 본다 — 작업트리의 임시 파일이 아니라
커밋에 실린 것만 문제 삼는다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SUSPICIOUS_SUFFIXES = (".db-journal", ".db-wal", ".db-shm", ".sqlite-journal")
"""sqlite 가 남기는 곁 파일 — 커밋될 이유가 없다."""


def tracked() -> list[str]:
    result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in result.stdout.split("\0") if name]


def test_tracked_files_are_discovered():
    """목록이 비면 아래 테스트가 공허하게 통과한다."""
    assert len(tracked()) > 50


def test_no_sqlite_marker_is_tracked():
    """`:memory:` 를 이름에 담은 파일은 테스트가 흘린 것이다."""
    stray = [name for name in tracked() if ":memory:" in name]

    assert not stray, f"sqlite 지시자를 이름에 담은 파일이 추적되고 있다: {stray}"


def test_no_sqlite_sidecar_is_tracked():
    """journal/wal 류는 실행 산출물이지 소스가 아니다."""
    stray = [name for name in tracked() if name.endswith(SUSPICIOUS_SUFFIXES)]

    assert not stray, f"sqlite 곁 파일이 추적되고 있다: {stray}"


@pytest.mark.parametrize("name", ["None", "runs.db"])
def test_no_accidental_root_artifact_is_tracked(name: str):
    """저장소 루트의 이런 이름은 테스트가 흘린 것이다 — 실제로 `None` 이 생긴 적 있다."""
    assert name not in tracked(), f"저장소 루트에 {name} 이 추적되고 있다"
