"""Deploying a graph as containers — validated, rolled back on failure, re-attached (#243)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from malkuth.authoring import Author
from malkuth.catalog import Catalog
from malkuth.core.agent import HealthState, HealthStatus
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.runtime.control import ControlClient
from malkuth.runtime.deployments import (
    DeploymentManager,
    DeploymentRecord,
    DeploymentStatus,
    InMemoryDeploymentStore,
    SqliteDeploymentStore,
)
from malkuth.runtime.docker.engine import DockerEngine
from malkuth.runtime.launcher import AgentLauncher
from malkuth.runtime.lifecycle import AgentState
from malkuth.runtime.ports import A2APortAllocator
from malkuth.runtime.spec import A2A_EDGES_ENV, A2A_PEERS_ENV, A2A_SECRET_ENV
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import until

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


class Tick:
    """health 루프용 — 즉시 통과시키면 루프가 hot-spin 해 to_thread 결과가 밀린다."""

    async def __call__(self, _delay: float) -> None:
        await asyncio.sleep(0.01)


@pytest.fixture
def healthy(monkeypatch):
    """모든 Control 클라이언트가 건강하다 — 감시 루프가 Ready 로 올린다."""

    async def well(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.HEALTHY)

    async def drained(self: ControlClient, *, timeout_s: float | None = None) -> None:
        return None

    monkeypatch.setattr(ControlClient, "health", well)
    # Ready 인 에이전트의 정지는 drain 을 먼저 친다 — 대역이 없으면 실제 HTTP 재시도로 느려진다
    monkeypatch.setattr(ControlClient, "drain", drained)


@pytest.fixture
def docker() -> TrackingDocker:
    return TrackingDocker()


@pytest.fixture
def manager(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    launcher = AgentLauncher(
        engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
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
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
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
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
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
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    assert sorted(second.launcher.launched) == [("alpha", 0), ("beta", 0)]
    adopted = second.launcher.launched[("alpha", 0)]
    # 살아 있다고 Ready 는 아니다 — 첫 health 성공이 올린다 (02 Rule 2)
    await until(lambda: adopted.lifecycle.accepts_tasks)
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
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
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
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    touched = await second.reattach()

    assert touched[0].status == DeploymentStatus.LOST
    assert "alpha" in (touched[0].error or "")
    # 붙은 쪽(beta)은 그대로 감시한다 — 그 컨테이너가 아직 선언으로 돌고 있으므로
    # 선언도 계속 보호한다 (리뷰: LOST 를 in_use 에서 빼면 덮어쓸 수 있다)
    assert list(second.launcher.launched) == [("beta", 0)]
    assert second.in_use("agent", "beta") and second.in_use("agent", "alpha")
    assert second.in_use("graph", "two")

    stopped = await second.teardown(record.deployment_id)

    assert stopped.status == DeploymentStatus.STOPPED
    assert second.launcher.launched == {}
    assert not second.in_use("agent", "beta")


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


# --- 선언 마운트와 A2A 배선 (#243) -------------------------------------------------


def wired_workspace(workspace: Path) -> None:
    """beta → alpha 를 선언한 그래프. 두 에이전트 모두 A2A 를 켠다."""
    for name in ("alpha", "beta"):
        doc = agent_doc(name)
        doc["spec"]["a2a"] = {"enabled": True}
        write(workspace / "agents" / name / "manifest.yaml", doc)
    graph = graph_doc("wired", ["alpha", "beta"])
    graph["spec"]["connections"] = [{"caller": "n1", "callee": "n0"}]
    write(workspace / "graphs" / "wired.yaml", graph)


def wired_manager(workspace: Path, docker: TrackingDocker, store=None) -> DeploymentManager:
    catalog = Catalog.under(workspace)
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker),
            ports=A2APortAllocator(port_range=(9100, 9110)),
            health_interval_s=10.0,
            health_sleep=Tick(),
        ),
        store=store or InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )


def env_of(docker: TrackingDocker, agent: str) -> dict[str, str]:
    for created in docker.created:
        if created["name"] == f"malkuth-{agent}-0":
            return created["environment"]
    raise AssertionError(f"{agent} was not created")


async def test_the_graph_connections_become_a2a_env_in_the_containers(workspace, docker, healthy):
    """03 Discovery — compose 가 손으로 적던 EDGES/SECRET/PEERS 를 runtime 이 준다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)

    record = await manager.deploy("wired")

    alpha, beta = env_of(docker, "alpha"), env_of(docker, "beta")
    assert alpha[A2A_EDGES_ENV] == beta[A2A_EDGES_ENV] == "beta>alpha"
    assert alpha[A2A_SECRET_ENV] == beta[A2A_SECRET_ENV] == record.a2a_secret
    assert len(record.a2a_secret) >= 32
    # caller 만 peer 주소를 받는다 — 상대의 컨테이너 이름과 **상대의** A2A 포트
    alpha_port = next(a.a2a_port for a in record.agents if a.name == "alpha")
    assert beta[A2A_PEERS_ENV] == f"alpha=malkuth-alpha-0:{alpha_port}"
    assert A2A_PEERS_ENV not in alpha
    await manager.launcher.stop_all()


async def test_declarations_are_mounted_read_only_into_the_base_image(workspace, docker, healthy):
    """02 Rule 2 — declarative agent 는 이미지를 굽지 않으므로 runtime 이 선언을 싣는다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)

    await manager.deploy("wired")

    volumes = next(c for c in docker.created if c["name"] == "malkuth-alpha-0")["volumes"]
    manifest = str((workspace / "agents" / "alpha" / "manifest.yaml").resolve())
    promptsets = str((workspace / "modules" / "promptsets").resolve())
    assert volumes[manifest] == {"bind": "/app/manifest.yaml", "mode": "ro"}
    assert volumes[promptsets] == {"bind": "/app/modules/promptsets", "mode": "ro"}
    # 없는 모듈 루트는 걸지 않는다 — Docker 가 root 소유 디렉토리를 만들어 버린다
    assert not any(v["bind"].endswith("/memorysets") for v in volumes.values())
    assert all(v["mode"] == "ro" for v in volumes.values())
    await manager.launcher.stop_all()


def test_runtime_a2a_env_names_match_agentd():
    """runtime 이 agentd 를 import 하지 않으므로 상수를 두 벌 둔다 — 드리프트 방지."""
    from malkuth.agentd import a2a_server

    assert (A2A_EDGES_ENV, A2A_SECRET_ENV, A2A_PEERS_ENV) == (
        a2a_server.EDGES_ENV,
        a2a_server.SECRET_ENV,
        a2a_server.PEERS_ENV,
    )


async def test_a_failed_deploy_returns_the_preallocated_ports(
    workspace, docker, healthy, monkeypatch
):
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    original = DockerEngine.start

    async def second_fails(self, spec):
        if spec.name == "malkuth-beta-0":
            raise MalkuthError(category=ErrorCategory.RUNTIME, code=ErrorCode.RT_001, message="x")
        return await original(self, spec)

    monkeypatch.setattr(DockerEngine, "start", second_fails)

    with pytest.raises(MalkuthError):
        await manager.deploy("wired")

    assert manager.launcher.ports is not None
    assert manager.launcher.ports.assigned == {}
    assert running(docker) == []


async def test_reattach_hands_the_launcher_what_a_restart_needs(workspace, docker, healthy):
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    first = wired_manager(workspace, docker, store)
    record = await first.deploy("wired")

    second = wired_manager(workspace, docker, store)
    await second.reattach()

    adopted = second.launcher.launched[("beta", 0)]
    args = adopted.restart_args
    assert args["manifest"].name == "beta"
    assert args["secrets"][A2A_SECRET_ENV] == record.a2a_secret
    assert args["secrets"][A2A_PEERS_ENV].endswith(f":{record.agents[0].a2a_port}")
    assert args["mounts"][0]["mount_path"] == "/app/manifest.yaml"
    # 기록된 포트를 그대로 잡는다 — 컨테이너 안의 env 는 이미 그 포트로 굳어 있다
    assert adopted.a2a_port == next(a.a2a_port for a in record.agents if a.name == "beta")
    await second.launcher.stop_all()


async def test_reattach_without_the_secrets_marks_the_deployment_lost(workspace, docker, healthy):
    """조용히 붙이면 첫 재시작에서 넘어진다 — 붙지 않고 이유를 남긴다."""
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    await wired_manager(workspace, docker, store).deploy("wired")
    second = wired_manager(workspace, docker, store)
    second.secrets_env = {}

    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.LOST]
    assert "ANTHROPIC_API_KEY" in (touched[0].error or "")
    assert second.launcher.launched == {}


async def test_the_sqlite_store_keeps_the_a2a_secret(tmp_path, workspace, docker, healthy):
    wired_workspace(workspace)
    store = SqliteDeploymentStore(path=tmp_path / "deployments.db")
    record = await wired_manager(workspace, docker, store).deploy("wired")

    reopened = SqliteDeploymentStore(path=tmp_path / "deployments.db")

    assert reopened.get(record.deployment_id).a2a_secret == record.a2a_secret


# --- 재시작이 바꾼 컨테이너를 기록이 따라간다 (#243) ----------------------------------


async def test_get_reflects_a_container_the_launcher_replaced(manager, docker):
    """health 재시작은 id 와 control 포트를 바꾼다 — 기록이 옛 컨테이너를 가리키면
    안 된다."""
    record = await manager.deploy("two")
    before = next(a for a in record.agents if a.name == "alpha")
    launched = manager.launcher.launched[("alpha", 0)]
    launched.lifecycle.transition(AgentState.UNHEALTHY)  # 재시작은 Unhealthy 에서 온다

    await manager.launcher._replace(launched)  # noqa: SLF001 — 재시작 경로를 직접 태운다

    after = next(a for a in manager.get(record.deployment_id).agents if a.name == "alpha")
    assert after.container_id != before.container_id
    assert after.container_id == manager.launcher.launched[("alpha", 0)].handle.container_id
    assert before.container_id in docker.removed
    await manager.launcher.stop_all()


async def test_reattach_finds_the_replaced_container_by_name(workspace, docker, healthy):
    """control plane 이 죽어 있는 동안 기록은 옛 id 를 들고 있다 — 이름으로 찾는다."""
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()

    def fresh() -> DeploymentManager:
        return DeploymentManager(
            catalog=catalog,
            author=Author(catalog=catalog),
            store=store,
            secrets_env={"ANTHROPIC_API_KEY": "k"},
            launcher=AgentLauncher(
                engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
            ),
            ready_poll_s=0.0,
            sleep=NoSleep(),
        )

    first = fresh()
    record = await first.deploy("two")
    replaced = first.launcher.launched[("alpha", 0)]
    replaced.lifecycle.transition(AgentState.UNHEALTHY)
    await first.launcher._replace(replaced)  # noqa: SLF001
    live_id = first.launcher.launched[("alpha", 0)].handle.container_id
    stale = store.get(record.deployment_id)
    assert next(a for a in stale.agents if a.name == "alpha").container_id != live_id

    second = fresh()
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    assert next(a for a in touched[0].agents if a.name == "alpha").container_id == live_id
    assert second.launcher.launched[("alpha", 0)].handle.container_id == live_id
    await first.launcher.stop_all()
    await second.launcher.stop_all()


# --- 리뷰(#250) 가 짚은 경계 -------------------------------------------------------


async def test_declarations_are_protected_while_agents_are_still_starting(
    workspace, docker, healthy, monkeypatch
):
    """`agents` 는 Ready 뒤에야 채워진다 — 기동 중에 in_use 가 False 면 authoring 이
    그 선언을 덮어쓴다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    gate = asyncio.Event()
    original = AgentLauncher.start

    async def slow_start(self, manifest, **kwargs):
        await gate.wait()
        return await original(self, manifest, **kwargs)

    monkeypatch.setattr(AgentLauncher, "start", slow_start)
    deploying = asyncio.create_task(manager.deploy("wired"))
    await asyncio.sleep(0)

    assert manager.in_use("agent", "alpha") and manager.in_use("agent", "beta")
    assert manager.in_use("graph", "wired")
    assert manager.deployments()[0].status == DeploymentStatus.STARTING

    gate.set()
    await deploying
    await manager.launcher.stop_all()


async def test_port_exhaustion_fails_the_deployment_and_frees_what_it_took(
    workspace, docker, healthy
):
    """준비 단계의 실패도 실패 경계 안이다 — 기록은 FAILED, 잡은 포트는 돌려준다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    assert manager.launcher.ports is not None
    manager.launcher.ports.port_range = (9100, 9100)  # 두 에이전트, 포트 하나

    with pytest.raises(MalkuthError) as exc_info:
        await manager.deploy("wired")

    assert exc_info.value.code == ErrorCode.RT_001
    assert manager.launcher.ports.assigned == {}
    assert manager.deployments()[0].status == DeploymentStatus.FAILED
    assert docker.created == []


# --- 저장소 스키마 마이그레이션 (#254) ---------------------------------------------


def test_the_sqlite_store_opens_a_database_from_before_the_new_columns(tmp_path):
    """실 스택에서 기동이 죽었다 — 옛 DB 는 `a2a_secret`/`declared` 컬럼이 없다."""
    import sqlite3

    path = tmp_path / "deployments.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE deployments (deployment_id TEXT PRIMARY KEY, graph TEXT NOT NULL, "
            "version TEXT NOT NULL, status TEXT NOT NULL, agents TEXT NOT NULL, error TEXT, "
            "updated_at TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO deployments VALUES (?,?,?,?,?,?,?)",
            ("dep-old", "two", "1.0.0", "ready", "[]", None, "2026-09-06T00:00:00+00:00"),
        )

    store = SqliteDeploymentStore(path=path)
    record = store.get("dep-old")

    assert record is not None and record.status == DeploymentStatus.READY
    assert record.a2a_secret == "" and record.declared == ()
    store.upsert(DeploymentRecord(**{**record.__dict__, "declared": ("alpha",), "a2a_secret": "s"}))
    assert store.get("dep-old").declared == ("alpha",)
    assert [r.deployment_id for r in store.list()] == ["dep-old"]
