"""agentd entrypoint — the container's main process.

컨테이너의 메인 프로세스. manifest 를 읽어 기동 시퀀스를 돌리고 Control API 를
서빙한다. 이미지가 이 모듈을 실행하므로, 에이전트별 이미지는 manifest 만 바꿔
끼우면 된다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
import yaml

from malkuth.agentd.server import AgentRuntime, create_app
from malkuth.core.agent import HealthState, HealthStatus
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest

if TYPE_CHECKING:
    from fastapi import FastAPI

CONTROL_PORT = 8080
"""Control port — 컨테이너 내부 고정 (02 Network). A2A 포트는 runtime 이 할당한다."""

DEFAULT_MANIFEST_PATH = "/app/manifest.yaml"
MANIFEST_ENV = "MALKUTH_MANIFEST"
TOKEN_ENV = "MALKUTH_AGENT_TOKEN"  # noqa: S105 — 값이 아니라 키 이름이다
EXECUTOR_ENV = "MALKUTH_EXECUTOR"

ECHO_EXECUTOR = "echo"
"""테스트 이미지가 선택하는 대역 — base 이미지의 기본값이 되어서는 안 된다."""

log = structlog.get_logger(__name__)


def load_manifest(path: Path) -> AgentManifest:
    """Load and validate the agent manifest.

    manifest 를 읽어 검증합니다. 미검증 manifest 로는 기동하지 않습니다
    (02 Manifest Rules 1).

    Args:
        path: Path to ``manifest.yaml``.

    Returns:
        The validated manifest.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the file is missing or invalid.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="cannot read agent manifest",
            details={"path": str(path)},
        ) from err

    try:
        return AgentManifest.model_validate(raw)
    except ValueError as err:
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="agent manifest failed validation",
            details={"path": str(path)},
        ) from err


def build_app(manifest: AgentManifest, executor: Any, *, token: str | None = None) -> FastAPI:
    """Build the Control API app for a manifest.

    manifest 에 대한 Control API 앱을 만듭니다.

    Args:
        manifest: The validated agent manifest.
        executor: The task executor serving invoke/stream.
        token: Per-agent token required on authenticated endpoints.

    Returns:
        The FastAPI application.
    """
    runtime = AgentRuntime(
        agent=manifest.name,
        executor=executor,
        card={
            "name": manifest.name,
            "version": manifest.metadata.version,
            "description": manifest.metadata.description,
            "capabilities": {"streaming": manifest.spec.a2a.capabilities.streaming},
        },
        health=lambda: HealthStatus(status=HealthState.HEALTHY),
        max_concurrent_tasks=manifest.spec.runtime.max_concurrent_tasks,
    )
    return create_app(runtime, token=token)


def build_executor(manifest: AgentManifest) -> Any:
    """Select the executor this image should serve.

    이 이미지가 서빙할 실행기를 고릅니다. 기본은 manifest 기반 표준 실행기이고,
    ``MALKUTH_EXECUTOR=echo`` 를 선언한 **테스트 이미지만** echo 대역을 씁니다 —
    base 이미지가 echo 로 동작하면 모든 에이전트가 대역이 되어버립니다.

    Args:
        manifest: The validated agent manifest.

    Returns:
        The executor serving invoke/stream.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the declared executor is unknown.
    """
    declared = os.environ.get(EXECUTOR_ENV, "").strip().lower()

    if declared == ECHO_EXECUTOR:
        from malkuth.agentd.echo import EchoExecutor

        return EchoExecutor()

    if declared:
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="unknown executor selection",
            agent=manifest.name,
            details={"executor": declared},
        )

    # 표준 경로: manifest 의 모듈/모델 선언을 따르는 실행기.
    # 모델 provider 바인딩(#14)이 서기 전까지는 기동 시점에 명확히 실패한다 —
    # 조용히 대역으로 떨어지면 운영에서 가짜 응답이 나간다
    raise MalkuthError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="no model provider bound for this agent image",
        agent=manifest.name,
        details={"hint": f"set {EXECUTOR_ENV}={ECHO_EXECUTOR} for the test image"},
    )


def main() -> None:
    """Run the agent daemon.

    에이전트 데몬을 실행합니다 — manifest 로드 → 실행기 선택 → Control API 서빙.
    """
    manifest = load_manifest(Path(os.environ.get(MANIFEST_ENV, DEFAULT_MANIFEST_PATH)))
    app = build_app(manifest, build_executor(manifest), token=os.environ.get(TOKEN_ENV))

    log.info(
        "agentd starting",
        agent=manifest.name,
        agent_version=manifest.metadata.version,
        port=CONTROL_PORT,
    )
    uvicorn.run(app, host="0.0.0.0", port=CONTROL_PORT, log_config=None)  # noqa: S104


if __name__ == "__main__":
    main()
