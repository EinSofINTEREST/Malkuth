"""Package-level export contract.

``__init__.py`` 의 re-export 가 **같은 이름의 하위 모듈을 가리지 않아야** 한다.
가려지면 monkeypatch·mock 대상 지정이 직관과 어긋나고, 모듈에 접근하려면
``sys.modules`` 우회가 필요해진다 (#87).
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest

SUBMODULES = ["agent", "errors", "events", "manifest", "skill"]


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_is_reachable_as_a_module(name):
    """``malkuth.core.<name>`` 가 전부 모듈로 잡혀야 한다 — 하나만 달라도 예측이 깨진다."""
    import malkuth.core

    importlib.import_module(f"malkuth.core.{name}")

    assert isinstance(getattr(malkuth.core, name), ModuleType)


def test_no_export_shadows_a_submodule():
    """re-export 이름이 하위 모듈명과 겹치면 그 모듈이 가려진다."""
    import malkuth.core

    collisions = sorted(set(malkuth.core.__all__) & set(SUBMODULES))

    assert collisions == [], f"이 심볼들이 동명 하위 모듈을 가린다: {collisions}"


def test_no_package_in_the_repository_shadows_its_submodules():
    """규칙을 core 에만 적용하면 다음 패키지에서 같은 문제가 되풀이된다.

    새 패키지가 생겨도 이 검사가 자동으로 잡는다.
    """
    import pkgutil

    import malkuth

    collisions = {}
    for found in pkgutil.walk_packages(malkuth.__path__, "malkuth."):
        if not found.ispkg:
            continue
        package = importlib.import_module(found.name)
        submodules = {m.name.rsplit(".", 1)[-1] for m in pkgutil.iter_modules(package.__path__)}
        clash = sorted(submodules & set(getattr(package, "__all__", [])))
        if clash:
            collisions[found.name] = clash

    assert collisions == {}, f"re-export 가 하위 모듈을 가린다: {collisions}"


def test_the_skill_decorator_is_still_importable():
    """가림을 없애느라 기존 import 경로를 깨면 안 된다."""
    from malkuth.core.skill import SkillContext, skill

    assert callable(skill)
    assert SkillContext is not None
