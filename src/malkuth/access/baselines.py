"""What declarations allow — the registry's second decision step.

선언이 기본 권한이다 (01 Access Control 1). 종류마다 선언이 사는 곳이 다르므로 종류별로 둔다.
선언 판정은 그것을 쓰는 강제 지점과 함께 연결한다 — 소비자 없는 판정은 강제와 따로 논다.

선언은 **요청마다 파일에서** 읽는다: 그룹 이동이나 mode 강등이 재시작 없이 다음 판정에 반영되어야
한다. 강제 지점의 캐시는 레지스트리가 선언 변경을 감지해 올리는 버전으로 무효화된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from malkuth.access.model import Mode
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.core.manifest import RESERVED_GLOBAL_GROUP, MemoryMode

if TYPE_CHECKING:
    from malkuth.catalog import Catalog
    from malkuth.core.manifest import GroupManifest

SPACE_ID = re.compile(r"^(?P<scope>local|group|global):(?P<owner>[^:\s*]+):(?P<alias>[^:\s*]+)$")
"""Memory Service 의 영구 space id — ``MemorySpace.space_id`` 와 같은 모양이다.

run scope 는 여기 없다: run space 는 run 이 경계이고 에이전트 신원만으로는 판정할 수 없다.
"""


@dataclass(frozen=True)
class MemoryBaseline:
    """Declared memory permissions — manifest (local), group.yaml (group), global.yaml (global).

    Attributes:
        catalog: 선언을 읽는 곳 — 매 판정마다 읽는다.
    """

    catalog: Catalog

    def allows(self, agent: str, target: str, mode: Mode | None) -> bool:
        matched = SPACE_ID.match(target)
        if matched is None:
            return False
        try:
            manifest = self.catalog.agent(agent)
        except MalkuthError as err:
            if err.code == ErrorCode.NF_001:
                return False
            raise
        scope, owner, alias = matched["scope"], matched["owner"], matched["alias"]
        writing = mode is Mode.RW

        if scope == "local":
            # 자기 이름의 space 만 — 남의 local space 는 선언으로 열리지 않는다
            return owner == agent and any(s.alias == alias for s in manifest.spec.memory.spaces)

        if scope == "group":
            if manifest.metadata.group != owner:
                return False  # 소속이 곧 경계다 — 비멤버는 읽기도 못 한다
            group = self._group(owner)
            return group is not None and any(
                s.alias == alias and (not writing or s.mode is MemoryMode.RW)
                for s in group.spec.memory.spaces
            )

        if owner != RESERVED_GLOBAL_GROUP:
            return False
        declared = self._group(RESERVED_GLOBAL_GROUP)
        return declared is not None and any(
            s.alias == alias and (not writing or agent in s.writers)
            for s in declared.spec.memory.spaces
        )

    def _group(self, name: str) -> GroupManifest | None:
        try:
            return self.catalog.group(name)
        except MalkuthError as err:
            if err.code == ErrorCode.NF_001:
                return None
            raise


__all__ = ["SPACE_ID", "MemoryBaseline"]
