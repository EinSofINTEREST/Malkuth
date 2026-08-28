"""Runs the Memory Service.

Memory Service 를 프로세스로 실행한다 — ``python -m malkuth.memory``.

09 Access Enforcement 1 이 요구하는 배치다: **저장소 자격증명은 이 프로세스만
갖고**, 에이전트는 불투명 토큰으로 HTTP 를 통해서만 닿는다. 이 진입점이
없으면 그 배치를 컨테이너로 세울 수 없다 (#181).
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
import uvicorn

from malkuth.config import load_config
from malkuth.memory.bootstrap import build_deployment
from malkuth.observability.metrics import DEFAULT_METRICS_PORT, Metrics, start_metrics_server

log = structlog.get_logger(__name__)

DEFAULT_PORT = 8090
DEFAULT_ROOT = "/repo"

PORT_ENV = "MALKUTH_MEMORY_PORT"
ROOT_ENV = "MALKUTH_REPO_ROOT"
ENVIRONMENT_ENV = "MALKUTH_ENV"
CONFIG_DIR_ENV = "MALKUTH_CONFIG_DIR"
LOG_LEVEL_ENV = "MALKUTH_LOG_LEVEL"
LOG_FORMAT_ENV = "MALKUTH_LOG_FORMAT"
METRICS_PORT_ENV = "MALKUTH_METRICS_PORT"
TOKENS_PATH_ENV = "MALKUTH_MEMORY_TOKENS_PATH"


def _setup_observability() -> Metrics:
    """Configure logging and expose metrics for this process.

    계측 코드가 있어도 여기서 registry 를 만들어 주입하지 않으면 런타임에서는
    아무 것도 흐르지 않는다 — agentd 와 같은 이유다.
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


def _publish_tokens(tokens: dict[str, str], path: Path) -> None:
    """Write the issued tokens where the runtime can pick them up.

    발급한 토큰을 runtime 이 집어갈 수 있는 곳에 씁니다.

    토큰은 자격증명이 아니라 **범위**를 담은 불투명 문자열이지만, 남의 것을
    쥐면 그 에이전트의 space 에 닿는다 — 소유자만 읽도록 좁힌다.
    """
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    path.chmod(0o600)
    log.info("memory tokens published", **{"agents": len(tokens)})


def main() -> None:
    """Run the Memory Service."""
    metrics = _setup_observability()
    config = load_config(
        os.environ.get(ENVIRONMENT_ENV, "dev"),
        config_dir=os.environ.get(CONFIG_DIR_ENV, "configs"),
    )
    root = Path(os.environ.get(ROOT_ENV, DEFAULT_ROOT))

    deployment = build_deployment(config, root=root, metrics=metrics)

    tokens_path = os.environ.get(TOKENS_PATH_ENV)
    if tokens_path:
        _publish_tokens(deployment.tokens, Path(tokens_path))

    port = int(os.environ.get(PORT_ENV, DEFAULT_PORT))
    log.info("memory service starting", port=port)
    uvicorn.run(deployment.app, host="0.0.0.0", port=port, log_config=None)  # noqa: S104


if __name__ == "__main__":
    main()
