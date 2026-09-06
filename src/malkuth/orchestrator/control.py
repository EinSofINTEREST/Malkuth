"""Control Plane HTTP surface for run operations.

01 은 Control Plane 의 책임으로 "run submission and result retrieval" 을
규정하지만 구현이 없었다 (🔭 Future). 이 표면이 그 최소 조각 —
**프로세스 밖에서 run 을 보고 조작하는 경로**를 연다 (#102).

조회와 drain 은 저장소만 있으면 되므로 어느 프로세스에서든 서빙할 수 있다.
Resume 은 다르다: 이어갈 state 가 구동 프로세스의 핸들에 있으므로 그 프로세스가
서빙해야 한다 — state 를 저장소에 복제하면 어느 쪽이 진실인지 모호해진다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.http_auth import require_token
from malkuth.http_errors import status_for
from malkuth.orchestrator.topology import GraphTopology
from malkuth.ui import UI_ROOT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from malkuth.authoring import Author
    from malkuth.catalog import Catalog, Listing
    from malkuth.deploy import ValidationReport
    from malkuth.orchestrator.runs import RunService
    from malkuth.orchestrator.runstore import RunRecord, RunStore
    from malkuth.runtime.deployments import DeploymentManager, DeploymentRecord


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
    catalog: Catalog | None = None,
    token: str | None = None,
    author: Author | None = None,
    deployments: DeploymentManager | None = None,
    runs: RunService | None = None,
) -> FastAPI:
    """Build the Control Plane app.

    run 조작 표면을 만듭니다.

    Args:
        store: Where runs are recorded — 다른 프로세스가 쓴 것도 보입니다.
        resume: Resumes a halted run; 이 프로세스가 그 run 을 구동할 때만
            제공됩니다. 없으면 resume 은 501 로 거절합니다 — 조용히 성공하면
            운영자가 재개됐다고 오해합니다.
        catalog: What the repository declares (#240). 없으면 카탈로그 라우트를
            열지 않는다 — 빈 목록을 돌려주면 "선언이 없다" 로 읽힌다.
        deployments: Turns graphs into running containers (#243). 없으면 배포
            라우트를 열지 않는다. 기동 시 살아 있는 컨테이너에 다시 붙는다.
        runs: Submits and resumes runs against deployments (#244). 있으면
            ``resume`` 콜백보다 우선한다 — 이 프로세스가 구동 프로세스다.
        author: Validates and writes graphs and manifests (#242). 없으면 쓰기
            라우트를 열지 않는다 — 읽기 전용 배포가 있을 수 있다.
        token: Bearer token every request must present (#241). None 이면 검사하지
            않는다 — 그것이 안전한지(loopback 인지)는 진입점이 판단한다.
            ``/v1/health`` 만 예외다 (02 API Rules 4 와 같은 이유).

    Returns:
        The FastAPI application.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 재시작 뒤 Docker 가 들고 있는 컨테이너를 기록과 대조해 다시 붙는다 —
        # 서버의 이벤트 루프 안에서 해야 health 감시 태스크가 살아남는다
        if deployments is not None:
            await deployments.reattach()
        try:
            yield
        finally:
            if runs is not None:
                await runs.close()

    app = FastAPI(title="Malkuth Control Plane", lifespan=lifespan)
    # 읽기도 보호한다 — 카탈로그에는 env_allowlist 같은 운영 정보가 있다
    api = APIRouter(dependencies=[Depends(require_token(token, realm="control plane token"))])

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        """무인증 — 살아 있는지만 답한다. 운영 정보는 싣지 않는다."""
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def _on_bad_body(_request: Request, err: RequestValidationError) -> JSONResponse:
        """본문이 JSON 도 아니거나 라우트 시그니처에 못 미치면 FastAPI 가 여기 오기 전에
        422 를 낸다 — 카탈로그·authoring 이 내는 `VAL_002` 와 같은 모양으로 맞춘다.
        UI 가 한 가지 모양만 다루게 (#242 리뷰)."""
        problem = MalkuthError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="request failed validation",
            details={
                "errors": [
                    {
                        "field": ".".join(str(loc) for loc in e.get("loc", ())),
                        "problem": e.get("msg", ""),
                    }
                    for e in err.errors()
                ]
            },
        )
        return JSONResponse(
            status_code=status_for(problem), content={"error": problem.payload().model_dump()}
        )

    @app.exception_handler(MalkuthError)
    async def _on_error(_request: Request, err: MalkuthError) -> JSONResponse:
        """구조화 에러를 상태 코드로 — 매핑은 공용 규칙을 따른다 (#234).

        여기서 자체 매핑을 갖고 있던 동안 저장소 실패(`STOR_003`)가 400 으로
        나갔다 — 서버 장애를 클라이언트 잘못으로 보고한 셈이다.
        """
        return JSONResponse(
            status_code=status_for(err), content={"error": err.payload().model_dump()}
        )

    @api.get("/v1/runs")
    async def list_runs(mode: str | None = None) -> list[dict[str, Any]]:
        """기록된 run 목록 — mode 로 좁힐 수 있습니다."""
        return [view_of(record).model_dump() for record in store.list(mode=mode)]

    @api.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """run 하나의 상태."""
        record = store.get(run_id)
        if record is None:
            raise unknown_run(run_id)
        view = view_of(record).model_dump()
        result = runs.result_of(run_id) if runs is not None else None
        if result is not None:
            # 완주한 mission 의 최종 state — 이 프로세스가 구동한 것만 안다
            view["state"] = result.state
            view["error"] = result.error.payload().model_dump() if result.error else None
        return view

    @api.post("/v1/runs/{run_id}/drain")
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

    @api.post("/v1/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, Any]:
        """Restart a halted run from its last iteration.

        재개는 **구동 프로세스만** 할 수 있습니다 — 이어갈 state 가 그 프로세스의
        핸들에 있기 때문입니다.
        """
        if store.get(run_id) is None:
            raise unknown_run(run_id)
        if runs is not None:
            resumed = await runs.resume(run_id)
            return {**view_of(resumed).model_dump(), "status": "resumed"}
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

    if catalog is not None:
        _mount_catalog(api, catalog)
    if author is not None:
        _mount_authoring(api, author)
    if deployments is not None:
        _mount_deployments(api, deployments)
    if runs is not None:
        _mount_runs(api, runs)

    app.include_router(api)
    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI) -> None:
    """운영자 화면 — 정적 파일. 인증은 화면이 아니라 화면이 부르는 `/v1/*` 에 있다.

    페이지 자체는 비밀이 아니다 (토큰 없이는 카탈로그도 못 읽는다). 별도 프로세스 없이
    같은 포트에서 서빙한다 (#245 기술 결정 — 빌드 없는 정적 자산).
    """
    app.mount("/ui", StaticFiles(directory=str(UI_ROOT), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")


def _mount_catalog(api: APIRouter, catalog: Catalog) -> None:
    """읽기 전용 카탈로그 — UI 가 "무엇으로 조립할 수 있는가" 를 보는 표면.

    목록은 파싱된 것과 **깨진 것을 함께** 돌려준다. 하나가 깨졌다고 전체를
    500 으로 답하면 운영자는 어느 파일인지 모른다.
    """

    def listing(found: Listing[Any], summary: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        return {
            "items": [summary(item) for item in found.items.values()],
            "problems": [asdict(problem) for problem in found.problems],
        }

    @api.get("/v1/agents")
    async def list_agents() -> dict[str, Any]:
        return listing(
            catalog.agents(),
            lambda m: {
                "name": m.name,
                "version": m.metadata.version,
                "group": m.metadata.group,
                "description": m.metadata.description,
                "model": {"provider": m.spec.model.provider, "name": m.spec.model.name},
            },
        )

    @api.get("/v1/agents/{name}")
    async def get_agent(name: str) -> dict[str, Any]:
        return catalog.agent(name).model_dump(mode="json")

    @api.get("/v1/graphs")
    async def list_graphs() -> dict[str, Any]:
        return listing(
            catalog.graphs(),
            lambda g: {
                "name": g.metadata.name,
                "version": g.metadata.version,
                "description": g.metadata.description,
                "mode": str(g.spec.mode),
                "goal": g.spec.goal,
                "nodes": len(g.spec.nodes),
            },
        )

    @api.get("/v1/graphs/{name}")
    async def get_graph(name: str) -> dict[str, Any]:
        return catalog.graph(name).model_dump(mode="json")

    @api.get("/v1/groups")
    async def list_groups() -> dict[str, Any]:
        return listing(
            catalog.groups(),
            lambda g: {
                "name": g.metadata.name,
                "description": g.metadata.description,
                "quotas": g.spec.quotas.model_dump(mode="json"),
            },
        )

    @api.get("/v1/groups/{name}")
    async def get_group(name: str) -> dict[str, Any]:
        return catalog.group(name).model_dump(mode="json")

    @api.get("/v1/modules/{module_type}")
    async def list_modules(module_type: str) -> dict[str, Any]:
        found = catalog.modules(module_type)
        return {
            "items": [
                {"name": name, "versions": list(versions)} for name, versions in found.items.items()
            ],
            "problems": [asdict(problem) for problem in found.problems],
        }

    @api.get("/v1/modules/{module_type}/{name}/{version}")
    async def get_module(module_type: str, name: str, version: str) -> dict[str, Any]:
        return catalog.module(module_type, name, version)


def _parsed[T: BaseModel](body: Any, model: type[T]) -> T:
    """요청 본문을 모델로 — 스키마 위반은 **어느 필드가 왜** 인지 담아 400 으로.

    FastAPI 의 기본 422 는 카탈로그가 깨진 파일에 대해 내는 형식과 다르다 —
    UI 가 한 가지 모양만 다루게 같은 `VAL_002` details 로 맞춘다. 본문을 ``Any`` 로
    받는 이유도 같다: ``dict`` 로 받으면 배열·문자열 본문이 여기 오기 전에 422 로 샌다.
    """
    try:
        return model.model_validate(body)
    except ValidationError as err:
        raise MalkuthError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="request body failed schema validation",
            details={
                "errors": [
                    {"field": ".".join(str(loc) for loc in e["loc"]), "problem": e["msg"]}
                    for e in err.errors()
                ]
            },
        ) from err


def _report(report: ValidationReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "findings": [
            {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
            for f in report.findings
        ],
    }


class Declarations(BaseModel):
    """`/v1/declarations` 본문 — 이름 → 문서. 이름이 곧 경로다."""

    graphs: dict[str, Any] = {}
    agents: dict[str, Any] = {}


class Draft(BaseModel):
    """`/v1/validate` 본문 — 저장하지 않을 초안들.

    모듈 수준에 두는 이유: `from __future__ import annotations` 아래에서 FastAPI 는
    annotation 을 문자열로 해석한다 — 함수 안의 클래스는 못 찾고 본문을 쿼리
    파라미터로 오해한다 (#241 의 `Request` 와 같은 함정).
    """

    graphs: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []


def _mount_authoring(api: APIRouter, author: Author) -> None:
    """검증 후 저장 — 검증 없이 쓰는 경로는 없다 (01 Contract Validation)."""

    @api.post("/v1/validate")
    async def validate(body: Annotated[Any, Body()]) -> dict[str, Any]:
        draft = _parsed(body, Draft)
        """초안을 저장된 것과 함께 검증만 한다 — 아무것도 쓰지 않는다."""
        return _report(
            author.validate(
                graphs=[_parsed(g, GraphTopology) for g in draft.graphs],
                agents=[_parsed(a, AgentManifest) for a in draft.agents],
            )
        )

    @api.put("/v1/declarations")
    async def put_declarations(body: Annotated[Any, Body()]) -> dict[str, Any]:
        """그래프와 에이전트를 **함께** 검증하고 함께 쓴다 — 에이전트 버전을 올리면서
        그것을 참조하는 그래프를 같이 올릴 때 따로는 어느 순서로도 통과하지 못한다."""
        draft = _parsed(body, Declarations)
        written = author.save_all(
            graphs={n: _parsed(g, GraphTopology) for n, g in draft.graphs.items()},
            agents={n: _parsed(a, AgentManifest) for n, a in draft.agents.items()},
        )
        return {"written": [str(p) for p in written]}

    @api.put("/v1/graphs/{name}")
    async def put_graph(name: str, body: Annotated[Any, Body()]) -> dict[str, Any]:
        path = author.save_graph(name, _parsed(body, GraphTopology))
        return {"name": name, "path": str(path)}

    @api.delete("/v1/graphs/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_graph(name: str) -> None:
        author.delete_graph(name)

    @api.put("/v1/agents/{name}")
    async def put_agent(name: str, body: Annotated[Any, Body()]) -> dict[str, Any]:
        path = author.save_agent(name, _parsed(body, AgentManifest))
        return {"name": name, "path": str(path)}

    @api.delete("/v1/agents/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_agent(name: str) -> None:
        author.delete_agent(name)


def _deployment_view(record: DeploymentRecord) -> dict[str, Any]:
    return {
        "deployment_id": record.deployment_id,
        "graph": record.graph,
        "version": record.version,
        "status": record.status,
        "error": record.error,
        "updated_at": record.updated_at,
        "agents": [
            {
                "name": a.name,
                "replica": a.replica,
                "container_id": a.container_id[:12],
                "image": a.image,
                "control_port": a.control_port,
                "a2a_port": a.a2a_port,
            }
            for a in record.agents
        ],
    }


class DeployRequest(BaseModel):
    graph: str


def _mount_deployments(api: APIRouter, deployments: DeploymentManager) -> None:
    """배포 lifecycle — 검증 → 실제 컨테이너 기동 → health → 기록 → 해체 (#243).

    토큰은 응답에 싣지 않는다 — 기록에는 있지만 API 로 내보낼 이유가 없다.
    """

    @api.post("/v1/deployments", status_code=status.HTTP_201_CREATED)
    async def create_deployment(body: Annotated[Any, Body()]) -> dict[str, Any]:
        request = _parsed(body, DeployRequest)
        return _deployment_view(await deployments.deploy(request.graph))

    @api.get("/v1/deployments")
    async def list_deployments() -> dict[str, Any]:
        return {"items": [_deployment_view(r) for r in deployments.deployments()]}

    @api.get("/v1/deployments/{deployment_id}")
    async def get_deployment(deployment_id: str) -> dict[str, Any]:
        return _deployment_view(deployments.get(deployment_id))

    @api.delete("/v1/deployments/{deployment_id}")
    async def delete_deployment(deployment_id: str) -> dict[str, Any]:
        return _deployment_view(await deployments.teardown(deployment_id))


class RunSubmission(BaseModel):
    deployment_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = None
    run_id: str | None = None


def _mount_runs(api: APIRouter, runs: RunService) -> None:
    """run 제출 — 주소는 배포가 안다 (#244). 제출은 즉시 돌아온다."""

    @api.post("/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    async def submit_run(body: Annotated[Any, Body()]) -> dict[str, Any]:
        request = _parsed(body, RunSubmission)
        record = await runs.submit(
            request.deployment_id, request.input, mode=request.mode, run_id=request.run_id
        )
        return view_of(record).model_dump()


__all__ = ["RunView", "create_app", "unknown_run", "view_of"]
