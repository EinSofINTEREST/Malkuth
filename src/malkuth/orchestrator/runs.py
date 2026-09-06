"""Submitting runs against a deployment (#244).

배포(#243)가 에이전트 주소를 알고 있으므로 run 제출자는 주소를 적지 않는다 —
`deployment_id` 만 준다. mission 은 완주를 기다리지 않고 run_id 를 즉시 돌려주며,
결과는 `GET /v1/runs/{id}` 로 본다 (긴 run 을 HTTP 로 붙잡지 않는다).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.runstore import RunRecord
from malkuth.orchestrator.topology import GraphMode
from malkuth.runtime.deployments import DeploymentStatus

if TYPE_CHECKING:
    from malkuth.catalog import Catalog
    from malkuth.orchestrator.runstore import RunStore
    from malkuth.orchestrator.submit import RunResult, RunSubmitter
    from malkuth.orchestrator.topology import GraphTopology
    from malkuth.runtime.control import ControlClient
    from malkuth.runtime.deployments import DeploymentManager, DeploymentRecord
    from malkuth.runtime.launcher import AgentLauncher

log = structlog.get_logger(__name__)


class RoutedClients(Mapping[str, "ControlClient"]):
    """`ControlNodeRuntime.clients` 자리에 들어가는, launcher 의 라우팅을 따르는 매핑.

    고정 dict 는 같은 레플리카만 계속 부른다 — `route` 는 준비된 레플리카 사이를
    round-robin 한다 (01 Scalability). 없는 에이전트는 KeyError — 노드 런타임이
    GRAPH_002 로 바꾼다.
    """

    def __init__(self, launcher: AgentLauncher) -> None:
        self._launcher = launcher

    def __getitem__(self, agent: str) -> ControlClient:
        try:
            return self._launcher.route(agent)
        except MalkuthError as err:
            if err.code == ErrorCode.RT_009:
                raise KeyError(agent) from err
            raise

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys(name for name, _ in self._launcher.launched))

    def __len__(self) -> int:
        return len({name for name, _ in self._launcher.launched})


def not_deployed_for_runs(deployment_id: str, *, status: str | None = None) -> MalkuthError:
    return MalkuthError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=("deployment is not ready to take runs" if status else "unknown deployment"),
        details={"deployment_id": deployment_id, **({"status": status} if status else {})},
    )


@dataclass
class RunService:
    """The control plane's run driver — 배포에 run 을 내고, 이어가고, 결과를 보여준다.

    mission 드라이버 태스크의 **소유자**다 (07 Async 5): fire-and-forget 이 아니라
    여기 목록에 있고, `close()` 가 정리한다.
    """

    catalog: Catalog
    deployments: DeploymentManager
    submitter: RunSubmitter
    store: RunStore
    drivers: dict[str, asyncio.Task[RunResult]] = field(default_factory=dict)
    results: dict[str, RunResult] = field(default_factory=dict)

    async def submit(
        self,
        deployment_id: str,
        initial_state: Mapping[str, Any],
        *,
        mode: str | None = None,
        run_id: str | None = None,
    ) -> RunRecord:
        deployment, topology = self._deployed(deployment_id)
        if mode is not None and GraphMode(mode) is not topology.spec.mode:
            raise MalkuthError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="requested mode differs from the graph's declared mode",
                details={"requested": mode, "declared": str(topology.spec.mode)},
            )
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        bound = log.bind(graph=topology.metadata.name, run_id=run_id, mode=str(topology.spec.mode))
        if topology.spec.mode is GraphMode.SERVICE:
            await self.submitter.start_service(topology, initial_state, run_id=run_id)
        else:
            self._drive(run_id, self.submitter.submit(topology, initial_state, run_id=run_id))
            # 기록은 드라이버가 첫 await 전에 남긴다 — 제출 응답이 그것을 보게 한다
            await asyncio.sleep(0)
        bound.info("run submitted", deployment_id=deployment.deployment_id)
        return self._record(run_id, topology)

    async def resume(self, run_id: str) -> RunRecord:
        record = self.store.get(run_id)
        if record is None:
            raise MalkuthError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown run",
                details={"run_id": run_id},
            )
        topology = self.catalog.graph(record.graph)
        if topology.spec.mode is GraphMode.SERVICE:
            await self.submitter.resume_service(topology, run_id)
        else:
            self.results.pop(run_id, None)
            self._drive(run_id, self.submitter.resume(topology, run_id))
            await asyncio.sleep(0)
        log.info("run resumed", graph=record.graph, run_id=run_id, mode=record.mode)
        return self._record(run_id, topology)

    def result_of(self, run_id: str) -> RunResult | None:
        """완주한 mission 의 결과 — 이 프로세스가 구동한 것만 안다."""
        return self.results.get(run_id)

    async def close(self) -> None:
        for task in list(self.drivers.values()):
            task.cancel()
        for task in list(self.drivers.values()):
            # 종료 중이다 — 취소된/실패한 드라이버의 결과는 버린다
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.submitter.stop_services()

    # --- 내부 --------------------------------------------------------------

    def _deployed(self, deployment_id: str) -> tuple[DeploymentRecord, GraphTopology]:
        deployment = self.deployments.get(deployment_id)
        if deployment.status != DeploymentStatus.READY:
            raise not_deployed_for_runs(deployment_id, status=str(deployment.status))
        topology = self.catalog.graph(deployment.graph)
        if topology.metadata.version != deployment.version:
            # 배포된 컨테이너는 그 버전의 선언으로 떴다 — 다른 버전으로 run 을 내면
            # 노드/에이전트 매핑이 어긋난 채 돈다
            raise MalkuthError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="graph version differs from the deployed one — redeploy first",
                details={
                    "graph": deployment.graph,
                    "deployed": deployment.version,
                    "current": topology.metadata.version,
                },
            )
        return deployment, topology

    def _record(self, run_id: str, topology: GraphTopology) -> RunRecord:
        return self.store.get(run_id) or RunRecord(
            run_id=run_id,
            graph=topology.metadata.name,
            mode=str(topology.spec.mode),
            status="running",
        )

    def _drive(self, run_id: str, driver: Awaitable[RunResult]) -> None:
        """mission 드라이버를 이 서비스 소유의 태스크로 띄운다 — 결과는 끝나는 순간 기록된다.

        done-callback 은 `gather` 보다 늦게 돌 수 있어 결과가 잠시 비어 보인다 —
        결과 기록을 코루틴 안에 두면 태스크 완료가 곧 기록 완료다.
        """

        async def run() -> RunResult:
            try:
                result = await driver
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — 드라이버의 예외는 여기서 끝난다 (05 Fail Gracefully)
                log.error("run driver failed", run_id=run_id, exc_info=err)
                raise
            finally:
                self.drivers.pop(run_id, None)
            self.results[run_id] = result
            return result

        self.drivers[run_id] = asyncio.create_task(run(), name=run_id)


__all__ = ["RoutedClients", "RunService"]
