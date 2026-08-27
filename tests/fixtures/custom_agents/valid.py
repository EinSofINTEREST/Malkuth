"""계약을 만족하는 커스텀 실행기 — entrypoint 배선 검증용."""

from __future__ import annotations

from typing import Any


class WithManifest:
    """manifest 를 받는 실행기 — 커스텀 코드가 선언을 읽어야 하는 경우."""

    def __init__(self, manifest: Any) -> None:
        self.manifest = manifest

    async def execute(self, task: Any) -> Any:
        return {"served_by": "WithManifest", "agent": self.manifest.name}

    async def stream(self, task: Any) -> Any:
        yield {"served_by": "WithManifest"}


class WithoutManifest:
    """인자 없는 실행기 — manifest 가 필요 없는 경우도 허용한다."""

    async def execute(self, task: Any) -> Any:
        return {"served_by": "WithoutManifest"}

    async def stream(self, task: Any) -> Any:
        yield {"served_by": "WithoutManifest"}


class MissingStream:
    """``stream`` 이 없는 실행기 — 계약 미충족."""

    async def execute(self, task: Any) -> Any:
        return {}


class NotCallableExecute:
    """``execute`` 가 호출 가능하지 않은 실행기."""

    execute = "not a method"

    async def stream(self, task: Any) -> Any:
        yield {}
