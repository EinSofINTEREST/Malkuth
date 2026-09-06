"""RunService — 배포에 run 을 내고 이어간다 (#244)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.orchestrator.run import RunStatus
from malkuth.orchestrator.runs import RoutedClients, RunService
from malkuth.orchestrator.runstore import InMemoryRunStore, RunRecord
from malkuth.orchestrator.submit import RunResult
from malkuth.orchestrator.topology import GraphMode
from malkuth.runtime.deployments import DeploymentRecord, DeploymentStatus
from tests.fixtures.topologies import make_mission, make_service


class FakeCatalog:
    def __init__(self, graphs: dict[str, Any]) -> None:
        self._graphs = graphs

    def graph(self, name: str):
        try:
            return self._graphs[name]
        except KeyError:
            raise MalkuthError(
                category="not_found", code=ErrorCode.NF_001, message=f"unknown graph: {name}"
            ) from None


class FakeDeployments:
    def __init__(self, records: dict[str, DeploymentRecord]) -> None:
        self.records = records

    def get(self, deployment_id: str) -> DeploymentRecord:
        try:
            return self.records[deployment_id]
        except KeyError:
            raise MalkuthError(
                category="not_found", code=ErrorCode.NF_001, message="unknown deployment"
            ) from None


@dataclass
class FakeSubmitter:
    """제출 계약만 흉내 낸다 — 무엇이 어떤 인자로 불렸는지 기록한다."""

    store: InMemoryRunStore
    calls: list[tuple[str, str]] = field(default_factory=list)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: bool = False

    async def submit(self, topology, initial_state, *, run_id=None) -> RunResult:
        self.calls.append(("submit", run_id))
        self.store.upsert(
            RunRecord(run_id=run_id, graph=topology.metadata.name, mode="mission", status="running")
        )
        await self.gate.wait()
        self.store.upsert(
            RunRecord(
                run_id=run_id, graph=topology.metadata.name, mode="mission", status="completed"
            )
        )
        return RunResult(
            run_id=run_id,
            graph=topology.metadata.name,
            mode=GraphMode.MISSION,
            status=RunStatus.COMPLETED,
            state={**initial_state, "report": "done"},
        )

    async def resume(self, topology, run_id, initial_state=None) -> RunResult:
        self.calls.append(("resume", run_id))
        return RunResult(
            run_id=run_id,
            graph=topology.metadata.name,
            mode=GraphMode.MISSION,
            status=RunStatus.COMPLETED,
            state={"resumed": True},
        )

    async def start_service(self, topology, initial_state, *, run_id=None, **_):
        self.calls.append(("start_service", run_id))
        self.store.upsert(
            RunRecord(run_id=run_id, graph=topology.metadata.name, mode="service", status="running")
        )

    async def resume_service(self, topology, run_id, **_):
        self.calls.append(("resume_service", run_id))

    async def stop_services(self) -> None:
        self.stopped = True


def deployed(graph: str, version: str = "1.0.0", status=DeploymentStatus.READY) -> DeploymentRecord:
    return DeploymentRecord(deployment_id="dep-1", graph=graph, version=version, status=status)


@pytest.fixture
def parts():
    store = InMemoryRunStore()
    mission = make_mission()
    service = make_service()
    catalog = FakeCatalog({mission.metadata.name: mission, service.metadata.name: service})
    submitter = FakeSubmitter(store=store)
    return store, catalog, submitter, mission, service


def service_for(parts, record: DeploymentRecord) -> RunService:
    store, catalog, submitter, _, _ = parts
    return RunService(
        catalog=catalog,
        deployments=FakeDeployments({"dep-1": record}),
        submitter=submitter,
        store=store,
    )


async def test_submit_returns_before_the_mission_finishes(parts):
    """긴 run 을 HTTP 로 붙잡지 않는다 — 제출은 즉시, 결과는 GET."""
    store, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))

    record = await runs.submit("dep-1", {"query": "q"})

    assert record.status == "running" and record.graph == mission.metadata.name
    assert runs.result_of(record.run_id) is None
    submitter.gate.set()
    await asyncio.gather(*runs.drivers.values())
    assert runs.result_of(record.run_id).state["report"] == "done"
    assert store.get(record.run_id).status == "completed"
    assert runs.drivers == {}


async def test_an_unknown_deployment_is_not_found(parts):
    runs = service_for(parts, deployed("x"))
    runs.deployments = FakeDeployments({})

    with pytest.raises(MalkuthError) as exc_info:
        await runs.submit("dep-nope", {})

    assert exc_info.value.code == ErrorCode.NF_001


async def test_a_deployment_that_is_not_ready_is_not_found(parts):
    _, _, _, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name, status=DeploymentStatus.STOPPED))

    with pytest.raises(MalkuthError) as exc_info:
        await runs.submit("dep-1", {})

    assert exc_info.value.code == ErrorCode.NF_001
    assert exc_info.value.details["status"] == "stopped"


async def test_a_graph_edited_since_the_deployment_is_refused(parts):
    """배포된 컨테이너는 그 버전의 선언으로 떴다 — 다른 버전으로 run 을 내지 않는다."""
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name, version="0.9.0"))

    with pytest.raises(MalkuthError) as exc_info:
        await runs.submit("dep-1", {})

    assert exc_info.value.code == ErrorCode.VAL_002
    assert submitter.calls == []


async def test_a_mode_that_contradicts_the_graph_is_refused(parts):
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))

    with pytest.raises(MalkuthError) as exc_info:
        await runs.submit("dep-1", {}, mode="service")

    assert exc_info.value.code == ErrorCode.VAL_002
    assert submitter.calls == []


async def test_a_service_graph_starts_its_loop(parts):
    _, _, submitter, _, service = parts
    runs = service_for(parts, deployed(service.metadata.name))

    record = await runs.submit("dep-1", {}, run_id="svc-1")

    assert submitter.calls == [("start_service", "svc-1")]
    assert record.mode == "service" and record.status == "running"


async def test_resume_drives_the_recorded_graph(parts):
    store, _, submitter, mission, service = parts
    runs = service_for(parts, deployed(mission.metadata.name))
    store.upsert(
        RunRecord(run_id="m-1", graph=mission.metadata.name, mode="mission", status="failed")
    )
    store.upsert(
        RunRecord(run_id="s-1", graph=service.metadata.name, mode="service", status="halted")
    )

    await runs.resume("m-1")
    await runs.resume("s-1")
    await asyncio.gather(*runs.drivers.values())

    assert submitter.calls == [("resume", "m-1"), ("resume_service", "s-1")]
    assert runs.result_of("m-1").state == {"resumed": True}


async def test_resume_of_an_unknown_run_is_not_found(parts):
    runs = service_for(parts, deployed("x"))

    with pytest.raises(MalkuthError) as exc_info:
        await runs.resume("nope")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_close_cancels_drivers_and_stops_services(parts):
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))
    await runs.submit("dep-1", {})
    assert runs.drivers

    await runs.close()

    assert runs.drivers == {} and submitter.stopped


def test_routed_clients_follow_the_launcher_and_miss_cleanly():
    """없는 에이전트는 KeyError → 노드 런타임이 GRAPH_002 로 바꾼다."""
    from malkuth.runtime.docker.engine import DockerEngine
    from malkuth.runtime.launcher import AgentLauncher
    from tests.fixtures.fake_docker import FakeDockerClient

    launcher = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))
    clients = RoutedClients(launcher)

    assert clients.get("planner") is None
    assert len(clients) == 0 and list(clients) == []
