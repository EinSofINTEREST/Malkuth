"""Runs the Control Plane.

Control Plane 을 프로세스로 실행한다 — ``python -m malkuth.orchestrator``.

01 은 Control Plane 의 책임으로 "run submission and result retrieval" 을 규정하고
`create_app` 이 그 표면을 갖고 있었는데, **그 앱을 만드는 곳이 테스트뿐이었다** —
`malkuth run-list` / `run-drain` / `run-resume` 세 명령이 붙을 서버가 없었다 (#221).

**이 프로세스가 하지 않는 일**: run 을 구동하지 않는다. 조회와 drain 은 저장소만
있으면 되지만 resume 은 다르다 — 이어갈 state 가 구동 프로세스의 핸들에 있다.
그래서 여기서 서빙하는 앱은 `resume` 없이 만들어지고, resume 요청은 501 로
거절한다. 조용히 성공해 운영자가 재개됐다고 믿는 편이 훨씬 나쁘다.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn

from malkuth.authoring import Author
from malkuth.catalog import Catalog
from malkuth.config import (
    DEFAULT_CONFIG_DIR,
    load_config,
    resolve_environment,
)
from malkuth.observability.metrics import DEFAULT_METRICS_PORT, Metrics, start_metrics_server
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.inuse import any_of, run_backed
from malkuth.orchestrator.runstore import SqliteRunStore

if TYPE_CHECKING:
    from malkuth.authoring import InUse
    from malkuth.catalog import Catalog
    from malkuth.orchestrator.runs import RunService
    from malkuth.runtime.deployments import DeploymentManager

log = structlog.get_logger(__name__)

CONFIG_DIR_ENV = "MALKUTH_CONFIG_DIR"
ROOT_ENV = "MALKUTH_REPO_ROOT"
MEMORY_URL_ENV = "MALKUTH_MEMORY_URL"
MEMORY_TOKENS_ENV = "MALKUTH_MEMORY_TOKENS_PATH"
"""카탈로그가 읽는 레포 루트 — memory 서비스와 같은 이름을 쓴다."""
LOG_LEVEL_ENV = "MALKUTH_LOG_LEVEL"
LOG_FORMAT_ENV = "MALKUTH_LOG_FORMAT"
METRICS_PORT_ENV = "MALKUTH_METRICS_PORT"


def _setup_observability() -> Metrics:
    """Configure logging and expose metrics for this process.

    계측 코드가 있어도 registry 를 만들어 주입하지 않으면 아무 것도 흐르지 않는다 —
    agentd / memory 와 같은 이유다.
    """
    from malkuth.observability.logging import configure

    configure(
        level=os.environ.get(LOG_LEVEL_ENV, "INFO"),
        json_output=os.environ.get(LOG_FORMAT_ENV, "json") == "json",
    )
    metrics = Metrics()
    start_metrics_server(
        int(os.environ.get(METRICS_PORT_ENV, DEFAULT_METRICS_PORT)),
        registry=metrics.registry,
    )
    return metrics


def main() -> None:
    """Serve the Control Plane over the configured run store."""
    _setup_observability()
    config = load_config(
        resolve_environment(),
        config_dir=os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR),
    )
    orchestrator = config.orchestrator

    if orchestrator.run_store is None:
        # 빈 목록을 돌려주면 운영자는 "run 이 없다" 고 읽는다 — 설정이 빠진 것과
        # 구분되지 않으므로 기동을 거부한다
        raise config_missing_store()

    if orchestrator.control_token is None:
        if not is_loopback(orchestrator.control_host):
            # 파일을 쓰고 컨테이너를 띄우는 표면을 무인증으로 밖에 열지 않는다
            raise config_missing_token(orchestrator.control_host)
        log.warning(
            "control plane has no token — allowed only because it binds to loopback",
            host=orchestrator.control_host,
        )

    store = SqliteRunStore(path=orchestrator.run_store)
    # 설정의 registry.roots 는 상대 경로다 — 작업 디렉토리가 아니라 레포 루트 기준
    root = Path(os.environ.get(ROOT_ENV, ".")).resolve()
    catalog = Catalog.from_config(config.registry.roots, base=root)
    deployments = _deployment_manager(config, catalog, store_root=root, orchestrator=orchestrator)
    # 실행 중 run 과 배포가 참조하는 선언은 지우거나 덮어쓰지 못한다 (#242 리뷰 / #243)
    pins: list[InUse] = [run_backed(store, catalog)]
    if deployments is not None:
        pins.append(deployments.in_use)
    author = Author(
        catalog=catalog,
        a2a_port_range=config.protocols.a2a.port_range,
        in_use=any_of(*pins),
    )
    if deployments is not None:
        # manager 는 검증에 author 를 쓴다 — 서로를 가리키므로 여기서 잇는다
        deployments.author = author
    runs = None if deployments is None else _run_service(config, catalog, deployments, store=store)
    log.info(
        "control plane starting",
        port=orchestrator.control_port,
        run_store=orchestrator.run_store,
        repo_root=str(root),
    )
    uvicorn.run(
        create_app(
            store,
            catalog=catalog,
            token=orchestrator.control_token,
            author=author,
            deployments=deployments,
            runs=runs,
        ),
        host=orchestrator.control_host,
        port=orchestrator.control_port,
        log_config=None,
    )


def _deployment_manager(
    config: Any, catalog: Catalog, *, store_root: Path, orchestrator: Any
) -> DeploymentManager | None:
    """배포 lifecycle 을 조립한다 — `deployment_store` 가 없으면 배포 표면을 열지 않는다.

    Docker 데몬은 SDK 가 환경(DOCKER_HOST)에서 찾는다. secrets 의 값 원천은 이 프로세스의
    환경변수다 — 무엇이 통과할지는 스코프 선언(allowlist / group / global)이 정한다.
    """
    if orchestrator.deployment_store is None:
        log.warning("deployments disabled — orchestrator.deployment_store is not set")
        return None
    from malkuth.runtime.deployments import DeploymentManager, SqliteDeploymentStore
    from malkuth.runtime.docker.client import SdkDockerClient
    from malkuth.runtime.docker.engine import DockerEngine
    from malkuth.runtime.launcher import AgentLauncher
    from malkuth.runtime.ports import A2APortAllocator

    launcher = AgentLauncher(
        engine=DockerEngine(client=SdkDockerClient(), network=config.runtime.network),
        ports=A2APortAllocator(port_range=config.protocols.a2a.port_range),
        health_interval_s=config.runtime.health_check.interval_s,
    )
    tokens_path = os.environ.get(MEMORY_TOKENS_ENV)
    memory_tokens: dict[str, str] = {}
    if tokens_path and Path(tokens_path).is_file():
        memory_tokens = json.loads(Path(tokens_path).read_text(encoding="utf-8"))
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog, a2a_port_range=config.protocols.a2a.port_range),
        launcher=launcher,
        store=SqliteDeploymentStore(path=orchestrator.deployment_store),
        secrets_env=dict(os.environ),
        agent_env=dict(config.runtime.agent_env),
        memory_url=os.environ.get(MEMORY_URL_ENV),
        memory_tokens=memory_tokens,
    )


def _run_service(
    config: Any, catalog: Catalog, deployments: DeploymentManager, *, store: SqliteRunStore
) -> RunService:
    """이 프로세스가 run 을 구동한다 — 노드 호출은 배포된 컨테이너로 라우팅된다 (#244)."""
    from malkuth.observability.metrics import Metrics
    from malkuth.orchestrator.checkpoint import build_checkpointer
    from malkuth.orchestrator.run import RunManager
    from malkuth.orchestrator.runs import RoutedClients, RunService
    from malkuth.orchestrator.submit import RunSubmitter
    from malkuth.runtime.nodes import ControlNodeRuntime

    orchestrator = config.orchestrator
    submitter = RunSubmitter(
        runtime=ControlNodeRuntime(clients=RoutedClients(deployments.launcher)),
        manager=RunManager(
            max_concurrent_runs=orchestrator.max_concurrent_runs,
            max_service_runs=orchestrator.max_service_runs,
            store=store,
        ),
        checkpointer=build_checkpointer(
            orchestrator.checkpointer, url=orchestrator.checkpointer_url
        ),
        node_timeout_s=orchestrator.node_timeout_s,
        metrics=Metrics(),
    )
    return RunService(catalog=catalog, deployments=deployments, submitter=submitter, store=store)


def is_loopback(host: str) -> bool:
    """bind 주소가 loopback 인가 — 이것만이 무인증을 허용하는 유일한 근거다."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def config_missing_token(host: str) -> Exception:
    """loopback 밖에 무인증으로 열면 누구나 컨테이너를 띄울 수 있다 — CFG_001 로 막는다."""
    from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

    return MalkuthError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="control plane bound outside loopback requires orchestrator.control_token",
        details={"setting": "orchestrator.control_token", "host": host},
    )


def config_missing_store() -> Exception:
    """저장소 없이 뜨면 조회가 조용히 비어 보인다 — CFG_001 로 막는다."""
    from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

    return MalkuthError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="control plane requires orchestrator.run_store",
        details={"setting": "orchestrator.run_store"},
    )


if __name__ == "__main__":  # pragma: no cover - 프로세스 진입점
    main()
