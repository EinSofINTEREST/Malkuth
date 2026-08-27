"""Control Plane HTTP surface for run operations.

01 은 Control Plane 의 책임으로 "run submission and result retrieval" 을
규정하지만 구현이 없었다 (🔭 Future). 이 표면이 그 최소 조각 —
**프로세스 밖에서 run 을 보고 조작하는 경로**를 연다 (#102).

조회와 drain 은 저장소만 있으면 되므로 어느 프로세스에서든 서빙할 수 있다.
Resume 은 다르다: 이어갈 state 가 구동 프로세스의 핸들에 있으므로 그 프로세스가
서빙해야 한다 — state 를 저장소에 복제하면 어느 쪽이 진실인지 모호해진다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

if TYPE_CHECKING:
    from collections.abc import Callable

    from malkuth.orchestrator.runstore import RunRecord, RunStore


def unknown_run(run_id: str) -> MalkuthError:
    """미지의 run — 조용히 200 을 돌려주면 호출자가 조작이 먹혔다고 오해한다."""
    return MalkuthError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=f"unknown run: {run_id}",
        details={"run_id": run_id},
    )


class RunView(BaseModel):
    """A run as the control plane reports it."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    graph: str
    mode: str
    status: str
    iteration: int
    failure_streak: int
    drain_requested: bool
    updated_at: str


def view_of(record: RunRecord) -> RunView:
    """저장 기록을 응답 표현으로."""
    return RunView(
        run_id=record.run_id,
        graph=record.graph,
        mode=record.mode,
        status=record.status,
        iteration=record.iteration,
        failure_streak=record.failure_streak,
        drain_requested=record.drain,
        updated_at=record.updated_at,
    )


def create_app(
    store: RunStore,
    *,
    resume: Callable[[str], Any] | None = None,
) -> FastAPI:
    """Build the Control Plane app.

    run 조작 표면을 만듭니다.

    Args:
        store: Where runs are recorded — 다른 프로세스가 쓴 것도 보입니다.
        resume: Resumes a halted run; 이 프로세스가 그 run 을 구동할 때만
            제공됩니다. 없으면 resume 은 501 로 거절합니다 — 조용히 성공하면
            운영자가 재개됐다고 오해합니다.

    Returns:
        The FastAPI application.
    """
    app = FastAPI(title="Malkuth Control Plane")

    @app.exception_handler(MalkuthError)
    async def _on_error(_request: Request, err: MalkuthError) -> JSONResponse:
        """구조화 에러를 상태 코드로 — 미지의 run 은 404."""
        code = (
            status.HTTP_404_NOT_FOUND
            if err.code is ErrorCode.NF_001
            else status.HTTP_400_BAD_REQUEST
        )
        return JSONResponse(status_code=code, content={"error": err.payload().model_dump()})

    @app.get("/v1/runs")
    async def list_runs(mode: str | None = None) -> list[dict[str, Any]]:
        """기록된 run 목록 — mode 로 좁힐 수 있습니다."""
        return [view_of(record).model_dump() for record in store.list(mode=mode)]

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """run 하나의 상태."""
        record = store.get(run_id)
        if record is None:
            raise unknown_run(run_id)
        return view_of(record).model_dump()

    @app.post("/v1/runs/{run_id}/drain")
    async def drain_run(run_id: str) -> dict[str, Any]:
        """Ask a run to stop after its current iteration.

        **요청만 남기고 즉시 반환합니다** — 진행 중 iteration 완료를 여기서
        기다리면 HTTP timeout 과 drain timeout 이 뒤엉킵니다. 실제 정지는
        구동 프로세스가 iteration 경계에서 수행합니다.
        """
        if not store.request_drain(run_id):
            raise unknown_run(run_id)
        record = store.get(run_id)
        if record is None:  # pragma: no cover - 방금 갱신했다
            raise unknown_run(run_id)
        return view_of(record).model_dump()

    @app.post("/v1/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, Any]:
        """Restart a halted run from its last iteration.

        재개는 **구동 프로세스만** 할 수 있습니다 — 이어갈 state 가 그 프로세스의
        핸들에 있기 때문입니다.
        """
        if store.get(run_id) is None:
            raise unknown_run(run_id)
        if resume is None:
            # 조용히 성공하면 운영자가 재개됐다고 믿고 손을 뗀다
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                content={
                    "error": {
                        "code": str(ErrorCode.GRAPH_001),
                        "message": "this control plane does not drive the run",
                        "run_id": run_id,
                    }
                },
            )

        handle = await resume(run_id)
        return {"run_id": getattr(handle, "run_id", run_id), "status": "resumed"}

    return app


__all__ = ["RunView", "create_app", "unknown_run", "view_of"]
