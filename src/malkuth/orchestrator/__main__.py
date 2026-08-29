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

import os

import structlog
import uvicorn

from malkuth.config import (
    DEFAULT_CONFIG_DIR,
    load_config,
    resolve_environment,
)
from malkuth.observability.metrics import DEFAULT_METRICS_PORT, Metrics, start_metrics_server
from malkuth.orchestrator.control import create_app
from malkuth.orchestrator.runstore import SqliteRunStore

log = structlog.get_logger(__name__)

CONFIG_DIR_ENV = "MALKUTH_CONFIG_DIR"
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
    orchestrator = load_config(
        resolve_environment(),
        config_dir=os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR),
    ).orchestrator

    if orchestrator.run_store is None:
        # 빈 목록을 돌려주면 운영자는 "run 이 없다" 고 읽는다 — 설정이 빠진 것과
        # 구분되지 않으므로 기동을 거부한다
        raise config_missing_store()

    store = SqliteRunStore(path=orchestrator.run_store)
    log.info(
        "control plane starting",
        port=orchestrator.control_port,
        run_store=orchestrator.run_store,
    )
    uvicorn.run(
        create_app(store),
        host=orchestrator.control_host,
        port=orchestrator.control_port,
        log_config=None,
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
