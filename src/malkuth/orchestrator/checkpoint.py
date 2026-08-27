"""Checkpointer configuration and error mapping.

Checkpointer 설정과 에러 변환. 노드 완료마다 state 를 저장해 실패 시
마지막 checkpoint 에서 재개할 수 있게 한다 — 데이터 손실 없는 재개가 목표다.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.telemetry import (
    OPERATION_LOAD,
    OPERATION_SAVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    CheckpointTelemetry,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from malkuth.observability.metrics import Metrics


class CheckpointerKind(StrEnum):
    """Checkpointer 백엔드 종류.

    dev 는 in-memory, 프로덕션은 외부 저장소를 쓴다 (오케스트레이터 다중 인스턴스 전제).
    """

    MEMORY = "memory"
    REDIS = "redis"
    POSTGRES = "postgres"


DEFAULT_CHECKPOINTER = CheckpointerKind.MEMORY


def _storage_error(code: ErrorCode, message: str, **details: Any) -> MalkuthError:
    """Checkpoint 저장소 실패를 STORAGE 카테고리로 변환한다."""
    return MalkuthError(
        category=ErrorCategory.STORAGE,
        code=code,
        message=message,
        retryable=True,
        details=details,
    )


def build_checkpointer(
    kind: CheckpointerKind | str = DEFAULT_CHECKPOINTER,
    *,
    url: str | None = None,
) -> BaseCheckpointSaver[Any]:
    """Build a checkpointer for the configured backend.

    설정된 백엔드의 checkpointer 를 만듭니다. ``default`` 는 프레임워크 기본값
    (in-memory) 으로 해석됩니다.

    Args:
        kind: Backend kind, or ``"default"`` to use the framework default.
        url: Connection URL required by external backends.

    Returns:
        A LangGraph checkpointer instance.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the backend is unknown or misconfigured.
    """
    try:
        resolved = DEFAULT_CHECKPOINTER if kind == "default" else CheckpointerKind(kind)
    except ValueError as err:
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"unknown checkpointer backend: {kind}",
            details={"checkpointer": str(kind)},
        ) from err

    if resolved is CheckpointerKind.MEMORY:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    if url is None:
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"checkpointer '{resolved}' requires a connection url",
            details={"checkpointer": str(resolved)},
        )

    # Redis/Postgres saver 는 선택 의존성 — 미설치 시 설정 오류로 보고한다
    try:
        if resolved is CheckpointerKind.REDIS:
            from langgraph.checkpoint.redis import RedisSaver  # type: ignore[import-not-found]

            saver = RedisSaver(redis_url=url)
            saver.setup()
            redis_saver: BaseCheckpointSaver[Any] = saver
            return redis_saver

        # **비동기 saver 여야 한다**: 오케스트레이터는 `ainvoke` 로 그래프를
        # 굴리는데 동기 PostgresSaver 는 `aget_tuple` 이 NotImplementedError 다.
        #
        # ``from_conn_string`` 은 context manager 를 돌려준다 — 그것을
        # checkpointer 로 쓰면 그래프에 물리는 순간 깨진다
        return cast("BaseCheckpointSaver[Any]", _deferred_postgres_saver(url))
    except ImportError as err:
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"checkpointer backend not installed: {resolved}",
            details={"checkpointer": str(resolved)},
        ) from err


def _deferred_postgres_saver(url: str) -> Any:
    """Build an async Postgres saver whose schema is prepared on first use.

    ``build_checkpointer`` 는 동기이고 CLI 는 ``asyncio.run`` 밖에서 부른다 —
    커넥션 열기와 ``setup()`` 은 모두 비동기라 여기서 미룬다.

    LangGraph 는 checkpointer 를 **isinstance 로 검사**하므로 덕타이핑 래퍼는
    통하지 않는다 — 실제 saver 를 상속한다.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    class DeferredAsyncPostgresSaver(AsyncPostgresSaver):
        """첫 사용 시 커넥션을 열고 스키마를 준비하는 saver."""

        def __init__(self) -> None:
            self._url = url
            self._prepared = False
            self._lock = asyncio.Lock()
            # 부모 생성자는 커넥션을 요구한다 — 준비 전에는 넘길 것이 없다
            self.conn = None  # type: ignore[assignment]
            self.pipe = None
            self.serde = AsyncPostgresSaver.serde

        async def _prepare(self) -> None:
            if self._prepared:
                return
            async with self._lock:
                if self._prepared:
                    return
                connection = await AsyncConnection.connect(
                    self._url, autocommit=True, row_factory=dict_row
                )
                AsyncPostgresSaver.__init__(self, conn=connection)
                await AsyncPostgresSaver.setup(self)
                self._prepared = True

        async def aget_tuple(self, config: Any) -> Any:
            await self._prepare()
            return await AsyncPostgresSaver.aget_tuple(self, config)

        async def alist(self, config: Any, **kwargs: Any) -> Any:
            await self._prepare()
            async for item in AsyncPostgresSaver.alist(self, config, **kwargs):
                yield item

        async def aput(self, config: Any, *args: Any, **kwargs: Any) -> Any:
            await self._prepare()
            return await AsyncPostgresSaver.aput(self, config, *args, **kwargs)

        async def aput_writes(self, config: Any, *args: Any, **kwargs: Any) -> Any:
            await self._prepare()
            return await AsyncPostgresSaver.aput_writes(self, config, *args, **kwargs)

    return DeferredAsyncPostgresSaver()


async def _guarded(
    action: Callable[[], Any],
    *,
    code: ErrorCode,
    message: str,
    operation: str,
    graph: str,
    run_id: str,
    metrics: Metrics | None,
) -> Any:
    """저장/복원 공통 경로 — 실패를 구조화하고 결과를 계측한다."""
    telemetry = None if metrics is None else CheckpointTelemetry(metrics)
    try:
        result = await action()
    except MalkuthError:
        if telemetry is not None:
            telemetry.operation(operation=operation, status=STATUS_FAILED)
        raise
    except Exception as err:
        if telemetry is not None:
            telemetry.operation(operation=operation, status=STATUS_FAILED)
        raise _storage_error(code, message, graph=graph, run_id=run_id) from err

    if telemetry is not None:
        telemetry.operation(operation=operation, status=STATUS_COMPLETED)
    return result


async def guarded_save(
    save: Callable[[], Any],
    *,
    graph: str,
    run_id: str,
    metrics: Metrics | None = None,
) -> Any:
    """Run a checkpoint save, converting failures to ``STOR_001``.

    Checkpoint 저장을 수행하고 실패를 ``STOR_001`` 로 변환합니다.

    Raises:
        MalkuthError: STORAGE/``STOR_001`` if the save fails.
    """
    return await _guarded(
        save,
        code=ErrorCode.STOR_001,
        message="checkpoint save failed",
        operation=OPERATION_SAVE,
        graph=graph,
        run_id=run_id,
        metrics=metrics,
    )


async def guarded_restore(
    restore: Callable[[], Any],
    *,
    graph: str,
    run_id: str,
    metrics: Metrics | None = None,
) -> Any:
    """Run a checkpoint restore, converting failures to ``STOR_002``.

    Checkpoint 복원을 수행하고 실패를 ``STOR_002`` 로 변환합니다.

    Raises:
        MalkuthError: STORAGE/``STOR_002`` if the restore fails.
    """
    return await _guarded(
        restore,
        code=ErrorCode.STOR_002,
        message="checkpoint restore failed",
        operation=OPERATION_LOAD,
        graph=graph,
        run_id=run_id,
        metrics=metrics,
    )
