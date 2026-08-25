"""Shared pytest configuration.

전역 테스트 설정 — fixture 는 tests/fixtures/ 에 두고 테스트가 직접 import 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 레포 루트를 import 루트로 노출해 `tests.fixtures` 패키지를 참조할 수 있게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
