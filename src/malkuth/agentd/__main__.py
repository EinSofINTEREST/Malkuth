"""agentd entrypoint — the container's main process.

컨테이너의 메인 프로세스. manifest 를 읽어 기동 시퀀스를 돌리고 Control API 를
서빙한다. 이미지가 이 모듈을 실행하므로, 에이전트별 이미지는 manifest 만 바꿔
끼우면 된다.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
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
from malkuth.memory.http import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
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

ARTIFACT_ROOT_ENV = "MALKUTH_ARTIFACT_ROOT"
"""Artifact 저장 루트 — runtime 이 주입한다. 미주입 시 저장소 없음."""

ARTIFACT_QUOTA_ENV = "MALKUTH_ARTIFACT_QUOTAS"
"""스코프별 바이트 상한 — ``local=1024,group=2048`` 형식. runtime 이 주입한다."""

GLOBAL_SCOPE = "global"
"""전역 artifact 스코프 이름 — 예약 그룹과 같은 이름을 쓴다."""

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


def load_entrypoint(manifest: AgentManifest) -> Any:
    """Load the custom executor a manifest declares.

    manifest 가 선언한 커스텀 실행기를 로드합니다 (02 Registering Agents 2).

    해석에 실패하면 **기동을 거부합니다** — 조용히 기본 실행기로 떨어지면
    운영자는 자기 코드가 도는 줄 알지만 실제로는 표준 루프가 돕니다.

    Args:
        manifest: The validated agent manifest.

    Returns:
        The custom executor instance.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the reference cannot be resolved or
            the loaded object does not satisfy the executor contract.
    """
    reference = manifest.spec.entrypoint or ""
    module_name, _, class_name = reference.partition(":")
    if not module_name or not class_name:
        raise _entrypoint_error(
            manifest, reference, reason="entrypoint must be in 'module:Class' format"
        )

    try:
        module = importlib.import_module(module_name)
        loaded = getattr(module, class_name)
    except (ImportError, AttributeError) as err:
        raise _entrypoint_error(manifest, reference, reason=type(err).__name__) from err

    try:
        instance = loaded(manifest) if _wants_manifest(loaded) else loaded()
    except Exception as err:
        # 생성자가 터지면 데몬이 구조화되지 않은 예외로 죽는다 — 운영자는
        # 설정 문제인지 코드 버그인지 구분할 단서를 잃는다
        raise _entrypoint_error(manifest, reference, reason=type(err).__name__) from err

    # 계약을 확인하지 않으면 첫 태스크에서야 AttributeError 로 터진다
    for required in ("execute", "stream"):
        if not callable(getattr(instance, required, None)):
            raise _entrypoint_error(manifest, reference, reason=f"executor is missing {required}()")

    log.info("custom executor loaded", agent=manifest.name, entrypoint=reference)
    return instance


def _wants_manifest(loaded: Any) -> bool:
    """생성자가 manifest 를 받는지 — 받지 않는 실행기도 허용한다."""
    try:
        signature = inspect.signature(loaded)
    except (TypeError, ValueError):  # pragma: no cover - 내장 타입 방어
        return False
    return len(signature.parameters) > 0


def _entrypoint_error(manifest: AgentManifest, reference: str, *, reason: str) -> MalkuthError:
    """entrypoint 해석 실패 — 기동을 막는다."""
    return MalkuthError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="agent entrypoint could not be loaded",
        agent=manifest.name,
        details={"entrypoint": reference, "reason": reason},
    )


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
    # manifest 가 커스텀 실행기를 선언했으면 그것이 이 이미지의 실행기다.
    # truthiness 로 보면 `entrypoint: ""` 가 **미선언과 같아져** 조용히 표준
    # 실행기로 떨어진다 — 선언한 이상 해석 실패로 거부해야 한다
    if manifest.spec.entrypoint is not None:
        return load_entrypoint(manifest)

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

    registry_tools = AgentToolRegistry(
        agent=manifest.name,
        skillsets=result.skillsets,
        memory=_memory_access(),
    )

    return Executor(
        agent=manifest.name,
        model=AnthropicModel(config=manifest.spec.model, agent=manifest.name),
        tools=registry_tools,
        render=lambda task: _render(result, task),
        tool_schemas=_executable_schemas(result, registry_tools),
        telemetry=_telemetry_for(manifest, metrics),
        artifacts=_artifact_store(manifest),
        output_keys=lambda task: _template_output_keys(result, task),
    )


def _artifact_store(manifest: AgentManifest) -> Any:
    """이 에이전트가 닿을 수 있는 artifact 스코프들 — 경로 미주입 시 None.

    02 Output Discipline 의 참조 전달 경로다. 주입하지 않으면 skill 이
    ``ctx.artifacts is None`` 을 받아 대용량 산출물을 남길 곳이 없다.

    스코프는 **소속이 정한다** (01 Resource Scoping): local 은 늘, group 은
    소속이 있을 때만, global 은 항상. 쓰기는 local 로만 가므로 에이전트가
    group/global 을 임의로 오염시킬 수 없다.
    """
    root = os.environ.get(ARTIFACT_ROOT_ENV)
    if not root:
        return None

    from malkuth.artifacts import FilesystemArtifactStore
    from malkuth.artifacts.scope import ArtifactScope, ScopedArtifacts

    base = Path(root)
    stores = {
        ArtifactScope.LOCAL: FilesystemArtifactStore(root=base, scope=manifest.name),
        ArtifactScope.GLOBAL: FilesystemArtifactStore(root=base, scope=GLOBAL_SCOPE),
    }
    group = manifest.metadata.group
    if group:
        stores[ArtifactScope.GROUP] = FilesystemArtifactStore(root=base, scope=group)

    return ScopedArtifacts(stores=stores, quotas=_artifact_quotas())


def _artifact_quotas() -> dict[Any, int]:
    """runtime 이 주입한 스코프별 상한.

    컨테이너는 ``groups/*.yaml`` 을 볼 수 없다 — 그것을 읽는 쪽이 값만
    넘겨준다 (02 Secrets Injection 과 같은 방향).
    """
    from malkuth.artifacts.scope import ArtifactScope

    declared = os.environ.get(ARTIFACT_QUOTA_ENV, "").strip()
    if not declared:
        return {}

    quotas: dict[Any, int] = {}
    for entry in declared.split(","):
        scope, _, limit = entry.partition("=")
        if scope.strip() and limit.strip().isdigit():
            quotas[ArtifactScope(scope.strip())] = int(limit)
    return quotas


def _memory_access() -> Any:
    """Build memory access from the injected endpoint.

    runtime 이 주소와 불투명 토큰을 주입했을 때만 만듭니다 — **DB 자격증명은
    컨테이너에 들어오지 않습니다** (09 Access Enforcement 1).
    """
    url = os.environ.get(MEMORY_URL_ENV)
    token = os.environ.get(MEMORY_TOKEN_ENV)
    if not url or not token:
        return None

    from malkuth.runtime.memory_http import HttpMemoryAccess

    return HttpMemoryAccess(base_url=url, token=token)


def _telemetry_for(manifest: AgentManifest, metrics: Metrics | None) -> ExecutorTelemetry | None:
    """이 에이전트의 계측기 — 메트릭 미주입 시 None.

    ``graph`` 는 여기서 정해지지 않는다: 태스크마다 다르므로 기록 시점에
    ``TraceContext`` 에서 읽는다 (#113).
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


def _template_output_keys(result: Any, task: Any) -> tuple[str, ...]:
    """이 태스크가 쓰는 템플릿이 요구하는 응답 키.

    계약은 **템플릿에** 붙어 있다 — 같은 에이전트가 노드마다 다른 키를 내야
    하고, 요구하는 곳과 선언하는 곳이 같아야 드리프트가 불가능하다 (#150).
    """
    if result.promptset is None:
        return ()
    template = result.promptset.manifest.spec.templates.get(task.template_name)
    return () if template is None else template.output_keys


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
    _serve(app, manifest, executor)


def _serve(app: Any, manifest: AgentManifest, executor: Any) -> None:
    """Control API 를 서빙하고, 선언되어 있으면 A2A 도 함께 띄운다.

    03 기동 순서 5단계 — 두 포트는 **별개 서버**다: Control 은 runtime 만,
    A2A 는 allowlist 된 peer 만 접근한다 (02 Network 5).
    """
    from malkuth.agentd.a2a_server import a2a_port, build_a2a_app

    peer_app = build_a2a_app(manifest, executor.execute)
    port = a2a_port()
    if peer_app is None or port is None:
        uvicorn.run(app, host="0.0.0.0", port=CONTROL_PORT, log_config=None)  # noqa: S104
        return

    asyncio.run(_serve_both(app, peer_app, port))


async def _serve_both(control: Any, peer: Any, peer_port: int) -> None:
    """두 서버를 함께 돌린다 — 하나가 끝나면 다른 하나도 정리한다."""
    servers = [
        uvicorn.Server(
            uvicorn.Config(control, host="0.0.0.0", port=CONTROL_PORT, log_config=None)  # noqa: S104
        ),
        uvicorn.Server(
            uvicorn.Config(peer, host="0.0.0.0", port=peer_port, log_config=None)  # noqa: S104
        ),
    ]
    async with asyncio.TaskGroup() as group:
        for server in servers:
            group.create_task(server.serve())


if __name__ == "__main__":
    main()
