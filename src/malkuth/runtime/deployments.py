"""Deploying a graph — validated, launched as real containers, torn down cleanly.

`AgentLauncher` 는 기동·감시·재시작을 다 갖고 있었지만 **소유자가 없었다** (#243).
컨테이너를 띄우는 것은 compose 뿐이었고, "그래프를 배포한다" 는 동작이 시스템에
존재하지 않았다. 여기가 그 소유자다 — control plane 프로세스가 이것을 들고 있다.

01 Deploy 단계를 그대로 따른다: 검증 → 기동 → health OK → 기록. 하나라도 실패하면
**띄운 것을 되감는다** — 부분 기동은 없다 (03 MCP Startup 과 같은 원칙).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.runtime.launcher import LaunchedAgent, MemoryEndpoint
from malkuth.runtime.scope import ScopedSecrets
from malkuth.runtime.spec import (
    A2A_EDGES_ENV,
    A2A_PEERS_ENV,
    A2A_SECRET_ENV,
    DEFAULT_CONTROL_PORT,
    container_name,
)

if TYPE_CHECKING:
    from malkuth.authoring import Author
    from malkuth.catalog import Catalog
    from malkuth.core.manifest import AgentManifest
    from malkuth.orchestrator.topology import GraphTopology
    from malkuth.runtime.launcher import AgentLauncher

log = structlog.get_logger(__name__)

DEFAULT_READY_TIMEOUT_S = 60.0
DEFAULT_READY_POLL_S = 0.5


class DeploymentStatus(StrEnum):
    """05 status 어휘를 따른다 — 실패는 한 이름으로."""

    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPED = "stopped"
    LOST = "lost"
    """재시작 후 컨테이너가 사라져 있었다 — 운영자가 알아야 하므로 지우지 않는다."""


@dataclass(frozen=True)
class DeployedAgent:
    """배포 안의 에이전트 하나 — 재시작 뒤 다시 붙을 수 있을 만큼만 기록한다.

    `token` 을 저장하는 이유: agentd 는 기동 시 받은 토큰을 바꿀 수 없다 (env).
    control plane 이 재시작한 뒤 그 컨테이너에 다시 말을 걸려면 같은 토큰이
    있어야 한다. 이 토큰은 사설 네트워크 안 Control API 의 per-agent 자격이고,
    저장소 파일은 이미 run 기록과 같은 신뢰 경계 안에 있다.
    """

    name: str
    replica: int
    container_id: str
    image: str
    control_port: int
    token: str
    a2a_port: int | None = None


@dataclass(frozen=True)
class DeploymentRecord:
    deployment_id: str
    graph: str
    version: str
    status: str
    agents: tuple[DeployedAgent, ...] = ()
    error: str | None = None
    updated_at: str = ""
    a2a_secret: str = ""
    """이 배포의 per-edge 토큰 서명 키 — 컨테이너들이 기동 시 받은 값이라
    재시작 뒤 같은 배포에 새 컨테이너를 세우려면 같은 키여야 한다."""


@runtime_checkable
class DeploymentStore(Protocol):
    """Where deployments are recorded so a restarted control plane can find them."""

    def upsert(self, record: DeploymentRecord) -> None: ...
    def get(self, deployment_id: str) -> DeploymentRecord | None: ...
    def list(self) -> Sequence[DeploymentRecord]: ...


def _storage_error(message: str, **details: str) -> MalkuthError:
    return MalkuthError(
        category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message=message, details=details
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InMemoryDeploymentStore:
    """테스트/단일 프로세스용."""

    def __init__(self) -> None:
        self._records: dict[str, DeploymentRecord] = {}

    def upsert(self, record: DeploymentRecord) -> None:
        self._records[record.deployment_id] = record

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        return self._records.get(deployment_id)

    def list(self) -> Sequence[DeploymentRecord]:
        return sorted(self._records.values(), key=lambda r: r.updated_at)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    graph         TEXT NOT NULL,
    version       TEXT NOT NULL,
    status        TEXT NOT NULL,
    agents        TEXT NOT NULL,
    error         TEXT,
    updated_at    TEXT NOT NULL,
    a2a_secret    TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class SqliteDeploymentStore:
    """runstore 와 같은 배치 — 파일 하나, 프로세스 재시작을 넘긴다."""

    path: str | Path
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(
                    str(self.path), isolation_level=None, check_same_thread=False
                )
                self._conn.execute(_SCHEMA)
            except sqlite3.Error as err:
                raise _storage_error(
                    "deployment store could not be opened", path=str(self.path)
                ) from err
        return self._conn

    def upsert(self, record: DeploymentRecord) -> None:
        try:
            self._connect().execute(
                "INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(deployment_id) DO UPDATE SET "
                "graph=excluded.graph, version=excluded.version, status=excluded.status, "
                "agents=excluded.agents, error=excluded.error, updated_at=excluded.updated_at, "
                "a2a_secret=excluded.a2a_secret",
                (
                    record.deployment_id,
                    record.graph,
                    record.version,
                    record.status,
                    json.dumps([a.__dict__ for a in record.agents]),
                    record.error,
                    record.updated_at,
                    record.a2a_secret,
                ),
            )
        except sqlite3.Error as err:
            raise _storage_error(
                "deployment could not be stored", deployment_id=record.deployment_id
            ) from err

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        row = (
            self._connect()
            .execute("SELECT * FROM deployments WHERE deployment_id = ?", (deployment_id,))
            .fetchone()
        )
        return _row(row) if row else None

    def list(self) -> Sequence[DeploymentRecord]:
        rows = self._connect().execute("SELECT * FROM deployments ORDER BY updated_at").fetchall()
        return [_row(r) for r in rows]


def _row(row: tuple[Any, ...]) -> DeploymentRecord:
    deployment_id, graph, version, status, agents, error, updated_at, a2a_secret = row
    return DeploymentRecord(
        deployment_id=deployment_id,
        graph=graph,
        version=version,
        status=status,
        agents=tuple(DeployedAgent(**a) for a in json.loads(agents)),
        error=error,
        updated_at=updated_at,
        a2a_secret=a2a_secret,
    )


def _agent_of(ref: str) -> str:
    return ref.split("/", 1)[1].split("@", 1)[0]


MANIFEST_MOUNT_PATH = "/app/manifest.yaml"
MODULES_MOUNT_PATH = "/app/modules"
"""agentd 의 기본 `MALKUTH_MANIFEST` / `MALKUTH_ROOT` 위치 — base 이미지 계약."""

MODULE_TYPES = ("skillsets", "promptsets", "memorysets")


@dataclass(frozen=True)
class Provision:
    """runtime 이 한 에이전트 컨테이너에 실어 보내는 것 — 선언(mounts)과 배선(env).

    03 Discovery: 에이전트는 peer 주소를 스스로 알아내지 않는다. 그래프의
    `connections` 를 edge/peer env 로 번역해 주는 곳이 여기다 — compose 가
    손으로 적던 값이다.
    """

    env: Mapping[str, str]
    mounts: tuple[Mapping[str, Any], ...]
    a2a_port: int | None


def not_deployed(deployment_id: str) -> MalkuthError:
    return MalkuthError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=f"unknown deployment: {deployment_id}",
        details={"deployment_id": deployment_id},
    )


@dataclass
class DeploymentManager:
    """Owns the launcher and turns graphs into running agent containers.

    Attributes:
        catalog: 그래프와 매니페스트를 읽는 곳.
        author: 배포 전 검증 (`Author.validate`) — 실패하면 아무것도 기동하지 않는다.
        launcher: 컨테이너를 실제로 띄우는 runtime.
        store: 배포 기록 — 프로세스 재시작을 넘겨야 한다.
        secrets_env: local/group/global 세 스코프의 값 원천. v0.1 은 control plane
            프로세스의 환경변수 하나다 — 스코프별 **선언**(allowlist / group.secrets /
            global.secrets)이 무엇을 통과시킬지 정하고, 값은 여기서 온다.
        agent_env: 모든 에이전트에 주입하는 **비밀이 아닌** 인프라 env (provider base
            URL 등). secrets 와 달리 allowlist 를 거치지 않는다 — 어디에 provider 가
            있는지는 자격증명이 아니라 배치다. 여기 secret 을 넣으면 allowlist 를 우회하는
            것이므로, 이름이 secret 패턴이면 거부한다.
        memory_url / memory_tokens: Memory Service 주소와 에이전트별 토큰 파일 내용.
        ready_timeout_s / poll: health 대기. 06 — sleep 은 주입한다.
    """

    catalog: Catalog
    author: Author
    launcher: AgentLauncher
    store: DeploymentStore
    secrets_env: Mapping[str, str] = field(default_factory=dict)
    agent_env: Mapping[str, str] = field(default_factory=dict)
    memory_url: str | None = None
    memory_tokens: Mapping[str, str] = field(default_factory=dict)
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S
    ready_poll_s: float = DEFAULT_READY_POLL_S
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        secretish = [k for k in self.agent_env if _looks_secret(k)]
        if secretish:
            raise MalkuthError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_002,
                message="agent_env must not carry secrets — those go through env_allowlist",
                details={"keys": secretish},
            )

    # --- 조회 --------------------------------------------------------------

    def get(self, deployment_id: str) -> DeploymentRecord:
        record = self.store.get(deployment_id)
        if record is None:
            raise not_deployed(deployment_id)
        return self._refresh(record)

    def deployments(self) -> Sequence[DeploymentRecord]:
        return [self._refresh(r) for r in self.store.list()]

    def in_use(self, kind: str, name: str) -> bool:
        """authoring 이 묻는다 — 배포 중인 선언은 지우거나 덮어쓰지 못한다 (#242)."""
        live = [
            r
            for r in self.store.list()
            if r.status in (DeploymentStatus.READY, DeploymentStatus.STARTING)
        ]
        if kind == "graph":
            return any(r.graph == name for r in live)
        return any(a.name == name for r in live for a in r.agents)

    # --- 배포 --------------------------------------------------------------

    async def deploy(self, graph_name: str) -> DeploymentRecord:
        """Validate, launch every agent, wait for health, record.

        01 Deploy: 검증 실패 시 아무것도 기동하지 않는다. 기동 중 하나라도 실패하면
        띄운 것을 전부 되감는다 — 유령 컨테이너는 05 Consistency 3 위반이다.
        """
        topology = self.catalog.graph(graph_name)
        report = self.author.validate(graphs=[topology], with_saved_graphs=False)
        if not report.ok:
            raise MalkuthError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_001,
                message="graph failed deployment validation",
                details={"graph": graph_name, "findings": [f.message for f in report.findings]},
            )

        deployment_id = f"dep-{uuid.uuid4().hex[:12]}"
        record = DeploymentRecord(
            deployment_id=deployment_id,
            graph=graph_name,
            version=topology.metadata.version,
            status=DeploymentStatus.STARTING,
            updated_at=_now(),
            a2a_secret=secrets.token_urlsafe(32),
        )
        self.store.upsert(record)
        bound = self._bind_log(record)

        manifests = self._agents_of(topology)
        provisions = self._provision(topology, manifests, a2a_secret=record.a2a_secret)
        launched: list[LaunchedAgent] = []
        try:
            for manifest in manifests:
                launched.append(await self._launch(manifest, provisions[manifest.name]))
            await self._wait_ready(launched)
        except BaseException as err:
            await self._rollback(launched)
            self._release_ports(provisions)
            failed = DeploymentRecord(
                **{
                    **record.__dict__,
                    "status": DeploymentStatus.FAILED,
                    "error": str(err),
                    "updated_at": _now(),
                }
            )
            self.store.upsert(failed)
            bound.error(
                "deployment failed and was rolled back", agents=len(launched), error_code=_code(err)
            )
            raise

        ready = DeploymentRecord(
            **{
                **record.__dict__,
                "status": DeploymentStatus.READY,
                "agents": tuple(
                    _deployed(a, self.launcher.issuer.known(a.agent) or "") for a in launched
                ),
                "updated_at": _now(),
            }
        )
        self.store.upsert(ready)
        bound.info("deployment ready", agents=len(launched))
        return ready

    async def teardown(self, deployment_id: str) -> DeploymentRecord:
        """Drain then stop every agent (02 Lifecycle 4·5). 기록은 남긴다."""
        record = self.get(deployment_id)
        if record.status in (DeploymentStatus.STOPPED, DeploymentStatus.FAILED):
            return record
        for agent in record.agents:
            await self.launcher.stop(agent.name)
        stopped = DeploymentRecord(
            **{**record.__dict__, "status": DeploymentStatus.STOPPED, "updated_at": _now()}
        )
        self.store.upsert(stopped)
        self._bind_log(stopped).info("deployment stopped", agents=len(record.agents))
        return stopped

    async def reattach(self) -> Sequence[DeploymentRecord]:
        """After a restart, find the containers we left running and pick them back up.

        Docker 가 컨테이너를 들고 있으므로 기록과 대조한다 (05 Consistency 3).
        사라진 컨테이너는 `lost` 로 표시한다 — 조용히 지우면 운영자가 모른다.
        """
        touched: list[DeploymentRecord] = []
        for record in self.store.list():
            if record.status != DeploymentStatus.READY:
                continue
            try:
                restart_args = self._restart_args_of(record)
            except MalkuthError as err:
                # 선언이나 secrets 가 사라졌으면 다시 세울 수 없다 — 붙지 않고
                # 운영자에게 보인다. 조용히 붙이면 첫 재시작에서 넘어진다
                self._mark_lost(record, f"cannot rebuild from declarations: {err.message}", touched)
                continue
            missing = []
            for agent in record.agents:
                # 이름으로 찾는다 — launcher 의 재시작이 컨테이너를 갈아 끼우면
                # id 와 control 포트가 바뀌지만 이름은 같은 자리를 가리킨다
                live = await self._live(agent)
                if live is None or not await self.launcher.adopt(
                    agent.name,
                    replica=agent.replica,
                    container_id=live[0],
                    image=agent.image,
                    control_port=live[1],
                    token=agent.token,
                    a2a_port=agent.a2a_port,
                    restart_args=restart_args[agent.name],
                ):
                    missing.append(agent.name)
            if missing:
                self._mark_lost(record, f"containers missing: {', '.join(missing)}", touched)
            else:
                touched.append(self._refresh(record))
        return touched

    async def _live(self, agent: DeployedAgent) -> tuple[str, int] | None:
        """이 자리에 지금 서 있는 컨테이너의 (id, control 포트) — 없으면 None."""
        client = self.launcher.engine.client
        container_id = await asyncio.to_thread(
            client.find, container_name(agent.name, agent.replica)
        )
        if container_id is None:
            return None
        try:
            port = await asyncio.to_thread(client.port_of, container_id, DEFAULT_CONTROL_PORT)
        except Exception:  # noqa: BLE001 — 포트가 없으면 붙을 수 없는 컨테이너다
            return None
        return container_id, port

    def _refresh(self, record: DeploymentRecord) -> DeploymentRecord:
        """launcher 가 아는 현재 컨테이너로 기록을 맞춘다 — 재시작이 id/포트를 바꾼다."""
        if record.status != DeploymentStatus.READY:
            return record
        agents = []
        for agent in record.agents:
            launched = self.launcher.launched.get((agent.name, agent.replica))
            agents.append(_deployed(launched, agent.token) if launched is not None else agent)
        if tuple(agents) == record.agents:
            return record
        refreshed = DeploymentRecord(
            **{**record.__dict__, "agents": tuple(agents), "updated_at": _now()}
        )
        self.store.upsert(refreshed)
        return refreshed

    def _mark_lost(
        self, record: DeploymentRecord, reason: str, touched: list[DeploymentRecord]
    ) -> None:
        lost = DeploymentRecord(
            **{
                **record.__dict__,
                "status": DeploymentStatus.LOST,
                "error": reason,
                "updated_at": _now(),
            }
        )
        self.store.upsert(lost)
        touched.append(lost)
        self._bind_log(lost).warning("deployment lost", reason=reason)

    # --- 내부 --------------------------------------------------------------

    def _agents_of(self, topology: GraphTopology) -> list[AgentManifest]:
        """노드별 에이전트 — 여러 노드가 같은 에이전트를 쓰면 **한 번만**."""
        seen: dict[str, AgentManifest] = {}
        for node in topology.spec.nodes:
            if node.agent is None:
                continue
            name = _agent_of(node.agent)
            if name not in seen:
                seen[name] = self.catalog.agent(name)
        return list(seen.values())

    def _provision(
        self,
        topology: GraphTopology,
        manifests: Sequence[AgentManifest],
        *,
        a2a_secret: str,
        ports: Mapping[str, int] | None = None,
    ) -> dict[str, Provision]:
        """그래프 하나의 에이전트 전부에 대한 선언 마운트와 A2A 배선.

        peer 주소에 상대의 포트가 들어가므로 포트는 **기동 전에 전부** 정한다.
        `ports` 를 주면(재부착) 할당하지 않고 그 값을 쓴다 — 컨테이너 안의 env 는
        이미 그 포트로 굳어 있다.
        """
        agent_of_node = {
            node.id: _agent_of(node.agent) for node in topology.spec.nodes if node.agent
        }
        edges = [
            (agent_of_node[c.caller], agent_of_node[c.callee])
            for c in topology.spec.connections
            if c.caller in agent_of_node and c.callee in agent_of_node
        ]
        assigned: dict[str, int] = dict(ports or {})
        if ports is None and self.launcher.ports is not None:
            for manifest in manifests:
                if manifest.spec.a2a.enabled:
                    assigned[manifest.name] = self.launcher.ports.allocate(manifest.name)

        provisions: dict[str, Provision] = {}
        for manifest in manifests:
            env: dict[str, str] = {}
            if edges:
                env[A2A_EDGES_ENV] = ",".join(f"{caller}>{callee}" for caller, callee in edges)
                env[A2A_SECRET_ENV] = a2a_secret
            peers = [
                f"{callee}={container_name(callee, 0)}:{assigned[callee]}"
                for caller, callee in edges
                if caller == manifest.name and callee in assigned
            ]
            if peers:
                env[A2A_PEERS_ENV] = ",".join(peers)
            provisions[manifest.name] = Provision(
                env=env, mounts=self._mounts(manifest.name), a2a_port=assigned.get(manifest.name)
            )
        return provisions

    def _restart_args_of(self, record: DeploymentRecord) -> dict[str, dict[str, Any]]:
        """기록된 배포의 컨테이너를 같은 선언으로 다시 세우는 데 필요한 것 전부."""
        topology = self.catalog.graph(record.graph)
        manifests = self._agents_of(topology)
        ports = {a.name: a.a2a_port for a in record.agents if a.a2a_port is not None}
        provisions = self._provision(topology, manifests, a2a_secret=record.a2a_secret, ports=ports)
        return {
            m.name: {
                "manifest": m,
                "secrets": self._env_for(m, provisions[m.name]),
                "memory": self._memory_for(m),
                "mounts": provisions[m.name].mounts,
            }
            for m in manifests
        }

    def _mounts(self, agent: str) -> tuple[Mapping[str, Any], ...]:
        """base 이미지에 선언을 들여보낸다 — manifest 하나와 모듈 루트들, 전부 읽기 전용.

        없는 모듈 루트는 걸지 않는다: Docker 는 없는 호스트 경로를 root 소유
        디렉토리로 만들어 버린다.
        """
        roots = self.catalog.roots
        mounts: list[Mapping[str, Any]] = [
            {
                "name": str((roots.agents / agent / "manifest.yaml").resolve()),
                "mount_path": MANIFEST_MOUNT_PATH,
                "read_only": True,
            }
        ]
        for module_type in MODULE_TYPES:
            root = roots.for_type(module_type).resolve()
            if root.is_dir():
                mounts.append(
                    {
                        "name": str(root),
                        "mount_path": f"{MODULES_MOUNT_PATH}/{module_type}",
                        "read_only": True,
                    }
                )
        return tuple(mounts)

    def _env_for(self, manifest: AgentManifest, provision: Provision) -> dict[str, str]:
        """컨테이너 env — 인프라(agent_env) < 스코프 secrets < 배선(provision) 순으로 겹친다."""
        scoped = ScopedSecrets.for_agent(
            manifest,
            groups=self.catalog.groups().items,
            local=self.secrets_env,
            group_values=self.secrets_env,
            global_values=self.secrets_env,
        ).env_for(tuple(manifest.spec.runtime.env_allowlist))
        return {**self.agent_env, **scoped, **provision.env}

    def _memory_for(self, manifest: AgentManifest) -> MemoryEndpoint | None:
        token = self.memory_tokens.get(manifest.name)
        if self.memory_url and token:
            return MemoryEndpoint(url=self.memory_url, token=token)
        return None

    async def _launch(self, manifest: AgentManifest, provision: Provision) -> LaunchedAgent:
        return await self.launcher.start(
            manifest,
            secrets=self._env_for(manifest, provision),
            memory=self._memory_for(manifest),
            mounts=provision.mounts,
            a2a_port=provision.a2a_port,
        )

    def _release_ports(self, provisions: Mapping[str, Provision]) -> None:
        """되감기 뒤 미리 잡아 둔 포트를 돌려준다 — 기동 전에 실패하면 stop 이 안 돌려준다."""
        if self.launcher.ports is None:
            return
        for agent, provision in provisions.items():
            if provision.a2a_port is not None:
                self.launcher.ports.release(agent)

    async def _wait_ready(self, launched: Sequence[LaunchedAgent]) -> None:
        deadline = self.clock() + self.ready_timeout_s
        while True:
            pending = [a.agent for a in launched if not a.lifecycle.accepts_tasks]
            if not pending:
                return
            if self.clock() >= deadline:
                raise MalkuthError(
                    category=ErrorCategory.RUNTIME,
                    code=ErrorCode.RT_002,
                    message="agents did not become healthy before the deploy deadline",
                    details={"pending": pending, "timeout_s": self.ready_timeout_s},
                    retryable=True,
                )
            await self.sleep(self.ready_poll_s)

    async def _rollback(self, launched: Sequence[LaunchedAgent]) -> None:
        for agent in launched:
            try:
                await self.launcher.stop(agent.agent, replica=agent.replica)
            except Exception as err:  # noqa: BLE001 — 되감기 중 하나가 실패해도 나머지를 계속 정리한다
                log.error(
                    "rollback could not stop an agent", agent=agent.agent, error_code=_code(err)
                )

    def _bind_log(self, record: DeploymentRecord) -> Any:
        return log.bind(deployment_id=record.deployment_id, graph=record.graph)


def _deployed(launched: LaunchedAgent, token: str) -> DeployedAgent:
    return DeployedAgent(
        name=launched.agent,
        replica=launched.replica,
        container_id=launched.handle.container_id,
        image=launched.handle.image,
        control_port=launched.handle.control_port,
        token=token,
        a2a_port=launched.a2a_port,
    )


def _code(err: BaseException) -> str:
    return str(getattr(err, "code", type(err).__name__))


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(
        marker in upper
        for marker in (
            "SECRET",
            "TOKEN",
            "PASSWORD",
            "PASSWD",
            "API_KEY",
            "APIKEY",
            "CREDENTIAL",
            "PRIVATE_KEY",
        )
    )


__all__ = [
    "DeployedAgent",
    "DeploymentManager",
    "DeploymentRecord",
    "DeploymentStatus",
    "DeploymentStore",
    "InMemoryDeploymentStore",
    "SqliteDeploymentStore",
    "not_deployed",
]
