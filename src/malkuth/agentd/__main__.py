"""agentd entrypoint — the container's main process.

컨테이너의 메인 프로세스. manifest 를 읽어 기동 시퀀스를 돌리고 Control API 를
서빙한다. 이미지가 이 모듈을 실행하므로, 에이전트별 이미지는 manifest 만 바꿔
끼우면 된다.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
import yaml

from malkuth.agentd.server import AgentRuntime, create_app
from malkuth.agentd.telemetry import ExecutorTelemetry
from malkuth.agentd.tools import AgentToolRegistry
from malkuth.core.agent import HealthState, HealthStatus
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import AgentManifest
from malkuth.core.skill import SkillSpec
from malkuth.core.tools import is_mcp_tool
from malkuth.memory.tool import MEMORY_SEARCH_TOOL
from malkuth.observability.metrics import DEFAULT_METRICS_PORT, Metrics
from malkuth.protocols.a2a.card import build_card

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI

CONTROL_PORT = 8080
"""Control port — 컨테이너 내부 고정 (02 Network). A2A 포트는 runtime 이 할당한다."""

DEFAULT_MANIFEST_PATH = "/app/manifest.yaml"
MANIFEST_ENV = "MALKUTH_MANIFEST"
TOKEN_ENV = "MALKUTH_AGENT_TOKEN"  # noqa: S105 — 값이 아니라 키 이름이다
EXECUTOR_ENV = "MALKUTH_EXECUTOR"

ECHO_EXECUTOR = "echo"

ROOT_ENV = "MALKUTH_ROOT"
DEFAULT_ROOT = "/app"
"""모듈 registry 루트 — 이미지가 modules/ 를 어디에 두는지."""

ANTHROPIC_PROVIDER = "anthropic"
"""이 이미지가 바인딩한 모델 provider — 다른 provider 선언은 CFG_001 로 거부한다."""

LOG_LEVEL_ENV = "MALKUTH_LOG_LEVEL"
LOG_FORMAT_ENV = "MALKUTH_LOG_FORMAT"
METRICS_PORT_ENV = "MALKUTH_METRICS_PORT"
"""관측 설정 — 컨테이너 안이라 env 로 받는다 (01 Multi-Environment Support)."""
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


def build_app(
    manifest: AgentManifest,
    executor: Any,
    *,
    token: str | None = None,
    tools: Sequence[Any] = (),
) -> FastAPI:
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
        # card 는 manifest 와 **실제 로드된 tool** 로부터 생성한다 — 손으로 쓰면
        # skill 목록이 빠지고 (03 AgentCard 1: 수동 작성 금지), peer 는 이
        # 에이전트가 뭘 할 수 있는지 알 수 없다
        card=build_card(manifest, tools).model_dump(mode="json"),
        health=lambda: HealthStatus(status=HealthState.HEALTHY),
        max_concurrent_tasks=manifest.spec.runtime.max_concurrent_tasks,
    )
    return create_app(runtime, token=token)


async def build_executor(manifest: AgentManifest, *, metrics: Metrics | None = None) -> Any:
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

    # 표준 경로: manifest 의 모듈/모델 선언을 따르는 실행기
    if manifest.spec.model.provider != ANTHROPIC_PROVIDER:
        # 조용히 대역으로 떨어지면 운영에서 가짜 응답이 나간다
        raise MalkuthError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="no model provider bound for this provider",
            agent=manifest.name,
            details={"provider": manifest.spec.model.provider},
        )

    from malkuth.agentd.bootstrap import Bootstrap
    from malkuth.agentd.executor import Executor
    from malkuth.agentd.providers.anthropic import AnthropicModel
    from malkuth.modules.promptset import PromptsetLoader
    from malkuth.modules.registry import ModuleRegistry
    from malkuth.modules.skillset import SkillsetLoader

    registry = ModuleRegistry.under(Path(os.environ.get(ROOT_ENV, DEFAULT_ROOT)))
    result = await Bootstrap(
        manifest,
        promptset_loader=PromptsetLoader(registry),
        skillset_loader=SkillsetLoader(registry),
    ).run()

    registry_tools = AgentToolRegistry(agent=manifest.name, skillsets=result.skillsets)

    return Executor(
        agent=manifest.name,
        model=AnthropicModel(config=manifest.spec.model, agent=manifest.name),
        tools=registry_tools,
        render=lambda task: _render(result, task),
        tool_schemas=_executable_schemas(result, registry_tools),
        telemetry=_telemetry_for(manifest, metrics),
    )


def _telemetry_for(manifest: AgentManifest, metrics: Metrics | None) -> ExecutorTelemetry | None:
    """이 에이전트의 계측기 — 메트릭 미주입 시 None.

    ``graph`` 라벨은 비워 둔다: 에이전트는 자신이 어느 그래프에 배선됐는지
    알지 못하고(02 Rule 6), 그것을 전달할 경로도 아직 없다 (#113).
    """
    if metrics is None:
        return None
    return ExecutorTelemetry(
        metrics,
        agent=manifest.name,
        group=manifest.metadata.group or "",
        provider=manifest.spec.model.provider,
        model=manifest.spec.model.name,
    )


def _executable_schemas(result: Any, tools: AgentToolRegistry) -> list[SkillSpec]:
    """Advertise only the tools this executor can actually run.

    실행할 수 없는 tool 을 모델에게 보이면 **tool 에러 루프**에 빠집니다 —
    모델이 고를 때마다 거부되고, 그 실패를 보고 다시 고릅니다.

    지금 빠지는 것:

    - ``memory_search`` — ``MemoryAccess`` 주입 경로가 아직 없습니다 (#111)
    - MCP tool — 세션이 없으면 부를 수 없습니다
    """
    runnable: list[SkillSpec] = []
    for name, spec in result.tools.items():
        if name == MEMORY_SEARCH_TOOL and tools.memory is None:
            continue
        if is_mcp_tool(name) and tools.mcp is None:
            # 세션이 없으면 부를 수 없다 — 실행할 수 없는 tool 을 광고하면
            # 모델이 고를 때마다 거부되어 루프에 빠진다
            continue
        runnable.append(spec)
    return runnable


def _render(result: Any, task: Any) -> str:
    """promptset 으로 태스크 프롬프트를 렌더링한다.

    ``node_id`` 가 없는 direct 요청은 ``default`` 템플릿을 쓴다
    (04 Compatibility Rules 4).
    """
    if result.promptset is None:
        raise MalkuthError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message="agent has no promptset to render with",
        )
    rendered: str = result.promptset.render(task.template_name, **task.input)
    return rendered


def _setup_observability() -> Metrics:
    """Configure logging and expose metrics for this process.

    로깅을 설정하고 메트릭을 노출합니다. 계측 로직이 있어도 여기서 registry 를
    만들어 주입하지 않으면 **런타임에서는 아무 것도 흐르지 않습니다** (#95).

    Returns:
        The process-wide metric registry.
    """
    from malkuth.observability.logging import configure
    from malkuth.observability.metrics import Metrics, start_metrics_server

    configure(
        level=os.environ.get(LOG_LEVEL_ENV, "INFO"),
        json_output=os.environ.get(LOG_FORMAT_ENV, "json") == "json",
    )

    metrics = Metrics()
    # registry 를 함께 넘겨야 한다 — 빠뜨리면 prometheus 기본 registry 를
    # 노출하게 되고, 우리가 채우는 것과 **다른 곳**이라 endpoint 가 늘 비어 있다
    start_metrics_server(
        int(os.environ.get(METRICS_PORT_ENV, DEFAULT_METRICS_PORT)),
        registry=metrics.registry,
    )
    return metrics


def main() -> None:
    """Run the agent daemon.

    에이전트 데몬을 실행합니다 — manifest 로드 → 실행기 선택 → Control API 서빙.
    """
    manifest = load_manifest(Path(os.environ.get(MANIFEST_ENV, DEFAULT_MANIFEST_PATH)))
    metrics = _setup_observability()
    executor = asyncio.run(build_executor(manifest, metrics=metrics))
    app = build_app(
        manifest,
        executor,
        token=os.environ.get(TOKEN_ENV),
        # 광고와 실행이 같은 목록을 봐야 peer 가 부를 수 없는 skill 을 보지 않는다
        tools=getattr(executor, "_tool_schemas", ()),
    )

    log.info(
        "agentd starting",
        agent=manifest.name,
        agent_version=manifest.metadata.version,
        port=CONTROL_PORT,
    )
    uvicorn.run(app, host="0.0.0.0", port=CONTROL_PORT, log_config=None)  # noqa: S104


if __name__ == "__main__":
    main()
