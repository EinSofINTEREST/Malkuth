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
    updated_at    TEXT NOT NULL
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
                "INSERT INTO deployments VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(deployment_id) DO UPDATE SET "
                "graph=excluded.graph, version=excluded.version, status=excluded.status, "
                "agents=excluded.agents, error=excluded.error, updated_at=excluded.updated_at",
                (
                    record.deployment_id,
                    record.graph,
                    record.version,
                    record.status,
                    json.dumps([a.__dict__ for a in record.agents]),
                    record.error,
                    record.updated_at,
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
    deployment_id, graph, version, status, agents, error, updated_at = row
    return DeploymentRecord(
        deployment_id=deployment_id,
        graph=graph,
        version=version,
        status=status,
        agents=tuple(DeployedAgent(**a) for a in json.loads(agents)),
        error=error,
        updated_at=updated_at,
    )


def _agent_of(ref: str) -> str:
    return ref.split("/", 1)[1].split("@", 1)[0]


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
        return record

    def deployments(self) -> Sequence[DeploymentRecord]:
        return self.store.list()

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
        )
        self.store.upsert(record)
        bound = self._bind_log(record)

        launched: list[LaunchedAgent] = []
        try:
            for manifest in self._agents_of(topology):
                launched.append(await self._launch(manifest))
            await self._wait_ready(launched)
        except BaseException as err:
            await self._rollback(launched)
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
            missing = []
            for agent in record.agents:
                if not await self.launcher.adopt(
                    agent.name,
                    replica=agent.replica,
                    container_id=agent.container_id,
                    image=agent.image,
                    control_port=agent.control_port,
                    token=agent.token,
                    a2a_port=agent.a2a_port,
                ):
                    missing.append(agent.name)
            if missing:
                lost = DeploymentRecord(
                    **{
                        **record.__dict__,
                        "status": DeploymentStatus.LOST,
                        "error": f"containers missing: {', '.join(missing)}",
                        "updated_at": _now(),
                    }
                )
                self.store.upsert(lost)
                touched.append(lost)
                self._bind_log(lost).warning("deployment lost its containers", missing=missing)
            else:
                touched.append(record)
        return touched

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

    async def _launch(self, manifest: AgentManifest) -> LaunchedAgent:
        groups = self.catalog.groups().items
        secrets = ScopedSecrets.for_agent(
            manifest,
            groups=groups,
            local=self.secrets_env,
            group_values=self.secrets_env,
            global_values=self.secrets_env,
        ).env_for(tuple(manifest.spec.runtime.env_allowlist))
        memory = None
        token = self.memory_tokens.get(manifest.name)
        if self.memory_url and token:
            memory = MemoryEndpoint(url=self.memory_url, token=token)
        return await self.launcher.start(
            manifest, secrets={**self.agent_env, **secrets}, memory=memory
        )

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
