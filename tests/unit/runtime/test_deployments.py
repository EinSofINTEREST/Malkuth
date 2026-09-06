"""Deploying a graph as containers — validated, rolled back on failure, re-attached (#243)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from malkuth.authoring import Author
from malkuth.catalog import Catalog
from malkuth.core.agent import HealthState, HealthStatus
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.runtime.control import ControlClient
from malkuth.runtime.deployments import (
    DeploymentManager,
    DeploymentStatus,
    InMemoryDeploymentStore,
    SqliteDeploymentStore,
)
from malkuth.runtime.docker.engine import DockerEngine
from malkuth.runtime.launcher import AgentLauncher
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def agent_doc(name: str) -> dict:
    return {
        "apiVersion": "malkuth/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "0.1.0"},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/solo@0.1.0"},
            "runtime": {"env_allowlist": ["ANTHROPIC_API_KEY"]},
        },
    }


def graph_doc(name: str, agents: list[str]) -> dict:
    nodes = [{"id": f"n{i}", "agent": f"agents/{a}@0.1.0"} for i, a in enumerate(agents)]
    edges = (
        [{"from": "START", "to": "n0"}]
        + [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(len(agents) - 1)]
        + [{"from": f"n{len(agents) - 1}", "to": "END"}]
    )
    return {
        "apiVersion": "malkuth/v1",
        "kind": "Graph",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "mode": "mission",
            "goal": "t",
            "state": {"schema": "malkuth.graphs.schemas:ResearchState"},
            "nodes": nodes,
            "edges": edges,
        },
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "malkuth/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {
                "engine": "jinja2",
                "templates": {
                    "default": {"file": "t.j2"},
                    "n0": {"file": "a.j2"},
                    "n1": {"file": "b.j2"},
                },
            },
        },
    )
    for name in ("alpha", "beta"):
        write(tmp_path / "agents" / name / "manifest.yaml", agent_doc(name))
    write(
        tmp_path / "groups" / "global.yaml",
        yaml.safe_load((REPO_ROOT / "groups" / "global.yaml").read_text(encoding="utf-8")),
    )
    write(tmp_path / "graphs" / "two.yaml", graph_doc("two", ["alpha", "beta"]))
    write(tmp_path / "graphs" / "twice.yaml", graph_doc("twice", ["alpha", "alpha"]))
    return tmp_path


def running(docker: FakeDockerClient) -> list[str]:
    """뜬 것 중 멈추거나 지워지지 않은 컨테이너 — 유령 컨테이너 검사의 기준."""
    gone = {cid for cid, _ in docker.stopped} | set(docker.removed)
    return [cid for cid in docker.started if cid not in gone]


class TrackingDocker(FakeDockerClient):
    """지워진 컨테이너는 inspect 에서 Running=False — 재시작 뒤 대조에 필요하다."""

    def inspect(self, container_id: str) -> dict:
        if container_id in self.removed:
            return {"Running": False, "ExitCode": 0, "OOMKilled": False}
        return super().inspect(container_id)


class NoSleep:
    async def __call__(self, _delay: float) -> None:
        await asyncio.sleep(0)


@pytest.fixture
def healthy(monkeypatch):
    """모든 Control 클라이언트가 건강하다 — 감시 루프가 Ready 로 올린다."""

    async def well(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.HEALTHY)

    monkeypatch.setattr(ControlClient, "health", well)


@pytest.fixture
def docker() -> TrackingDocker:
    return TrackingDocker()


@pytest.fixture
def manager(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    launcher = AgentLauncher(
        engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=NoSleep()
    )
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=launcher,
        store=InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_timeout_s=5.0,
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )


# --- 배포 ------------------------------------------------------------------------


async def test_deploy_launches_every_agent_and_becomes_ready(manager, docker):
    record = await manager.deploy("two")

    assert record.status == DeploymentStatus.READY
    assert sorted(a.name for a in record.agents) == ["alpha", "beta"]
    assert len(docker.created) == 2
    assert all(a.container_id and a.token for a in record.agents)
    await manager.launcher.stop_all()


async def test_the_same_agent_on_two_nodes_is_launched_once(manager, docker):
    record = await manager.deploy("twice")

    assert [a.name for a in record.agents] == ["alpha"]
    assert len(docker.created) == 1
    await manager.launcher.stop_all()


async def test_a_graph_that_fails_validation_launches_nothing(manager, docker, workspace):
    write(workspace / "graphs" / "bad.yaml", graph_doc("bad", ["nobody"]))

    with pytest.raises(MalkuthError) as excinfo:
        await manager.deploy("bad")

    assert excinfo.value.code == ErrorCode.VAL_001
    assert docker.created == []
    assert manager.deployments() == []


async def test_an_unknown_graph_is_not_found(manager):
    with pytest.raises(MalkuthError) as excinfo:
        await manager.deploy("nope")

    assert excinfo.value.code == ErrorCode.NF_001


# --- 되감기 -----------------------------------------------------------------------


async def test_a_launch_failure_rolls_back_what_was_started(manager, docker, monkeypatch):
    """유령 컨테이너는 05 Consistency 3 위반 — 하나가 못 뜨면 앞서 뜬 것을 내린다."""
    original = docker.create
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("daemon hiccup")
        return original(**kwargs)

    monkeypatch.setattr(docker, "create", flaky)

    with pytest.raises(MalkuthError):
        await manager.deploy("two")

    record = manager.deployments()[0]
    assert record.status == DeploymentStatus.FAILED
    assert record.error
    assert running(docker) == [], "먼저 뜬 컨테이너가 남았다"


async def test_agents_that_never_get_healthy_time_out_and_roll_back(workspace, docker, monkeypatch):
    async def sick(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.UNHEALTHY)

    monkeypatch.setattr(ControlClient, "health", sick)
    catalog = Catalog.under(workspace)
    ticks = iter(range(0, 100, 10))
    manager = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=NoSleep()
        ),
        store=InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_timeout_s=30.0,
        ready_poll_s=0.0,
        sleep=NoSleep(),
        clock=lambda: next(ticks),
    )

    with pytest.raises(MalkuthError) as excinfo:
        await manager.deploy("two")

    assert excinfo.value.code == ErrorCode.RT_002
    assert running(docker) == []
    assert manager.deployments()[0].status == DeploymentStatus.FAILED


# --- 해체 / in_use ---------------------------------------------------------------


async def test_teardown_stops_every_agent_and_keeps_the_record(manager, docker):
    record = await manager.deploy("two")

    stopped = await manager.teardown(record.deployment_id)

    assert stopped.status == DeploymentStatus.STOPPED
    assert running(docker) == []
    assert manager.get(record.deployment_id).status == DeploymentStatus.STOPPED


async def test_teardown_of_an_unknown_deployment_is_not_found(manager):
    with pytest.raises(MalkuthError) as excinfo:
        await manager.teardown("dep-nope")

    assert excinfo.value.code == ErrorCode.NF_001


async def test_in_use_follows_the_deployment(manager):
    assert not manager.in_use("agent", "alpha")
    record = await manager.deploy("two")

    assert manager.in_use("agent", "alpha") and manager.in_use("graph", "two")
    assert not manager.in_use("agent", "gamma")

    await manager.teardown(record.deployment_id)
    assert not manager.in_use("agent", "alpha")


async def test_authoring_refuses_to_touch_a_deployed_agent(manager, workspace):
    """#242 의 in_use 훅이 실제로 이것으로 채워진다."""
    await manager.deploy("two")
    author = Author(catalog=Catalog.under(workspace), in_use=manager.in_use)

    with pytest.raises(MalkuthError) as excinfo:
        author.delete_graph("two")

    assert "deployed" in excinfo.value.message
    await manager.launcher.stop_all()


# --- 재시작 --------------------------------------------------------------------------


async def test_a_restarted_control_plane_reattaches_running_containers(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()
    first = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=NoSleep()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    record = await first.deploy("two")
    assert record.status == DeploymentStatus.READY

    # 재시작 — 같은 저장소, 같은 Docker, 새 launcher
    second = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=NoSleep()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    assert sorted(second.launcher.launched) == [("alpha", 0), ("beta", 0)]
    assert second.launcher.route("alpha").agent == "alpha"
    assert second.in_use("agent", "alpha")
    await second.launcher.stop_all()


async def test_missing_containers_mark_the_deployment_lost(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()
    first = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=NoSleep()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    record = await first.deploy("two")
    docker.remove(record.agents[0].container_id)  # 누가 밖에서 지웠다

    second = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=NoSleep()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    touched = await second.reattach()

    assert touched[0].status == DeploymentStatus.LOST
    assert "alpha" in (touched[0].error or "")
    assert not second.in_use("agent", "alpha"), "lost 는 배포 중이 아니다"
    await second.launcher.stop_all()


# --- 저장소 ----------------------------------------------------------------------


async def test_the_sqlite_store_survives_a_reopen(tmp_path, manager):
    manager.store = SqliteDeploymentStore(path=str(tmp_path / "dep.db"))
    record = await manager.deploy("two")

    reopened = SqliteDeploymentStore(path=str(tmp_path / "dep.db"))

    assert reopened.get(record.deployment_id) == record
    assert [r.deployment_id for r in reopened.list()] == [record.deployment_id]
    await manager.launcher.stop_all()


# --- agent_env 는 secret 을 우회하지 못한다 ------------------------------------------


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "DB_PASSWORD", "MY_SECRET", "X_TOKEN"])
def test_agent_env_refuses_secret_looking_keys(workspace, docker, key):
    catalog = Catalog.under(workspace)
    with pytest.raises(MalkuthError) as excinfo:
        DeploymentManager(
            catalog=catalog,
            author=Author(catalog=catalog),
            store=InMemoryDeploymentStore(),
            launcher=AgentLauncher(engine=DockerEngine(client=docker)),
            agent_env={key: "x"},
        )
    assert excinfo.value.code == ErrorCode.CFG_002


async def test_agent_env_reaches_the_container_alongside_secrets(manager, docker):
    manager.agent_env = {"ANTHROPIC_BASE_URL": "http://fake:8000"}

    await manager.deploy("two")

    env = docker.created[0]["environment"]
    assert env["ANTHROPIC_BASE_URL"] == "http://fake:8000"
    assert env["ANTHROPIC_API_KEY"] == "k"
    await manager.launcher.stop_all()
