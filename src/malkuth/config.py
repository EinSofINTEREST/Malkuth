"""Framework configuration.

환경별 설정 로딩과 스키마 검증. 잘못된 설정으로는 기동하지 않는다 —
런타임 중에 발견되는 설정 오류는 이미 늦다 (기동 시 FATAL).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "MALKUTH_"
"""환경변수 오버라이드 접두사 — 중첩 키는 ``__`` 로 구분한다."""

NESTED_SEPARATOR = "__"
DEFAULT_CONFIG_DIR = "configs"


def config_error(message: str, **details: Any) -> MalkuthError:
    """설정 실패를 구조화 에러로 만든다 — 기동을 막는 FATAL 경로다."""
    return MalkuthError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message=message,
        details=details,
    )


class ResourceDefaults(BaseModel):
    """Default container resources."""

    model_config = ConfigDict(frozen=True)

    cpu: str = "1.0"
    memory: str = "1Gi"


class HealthCheckConfig(BaseModel):
    """Agent health polling."""

    model_config = ConfigDict(frozen=True)

    interval_s: float = Field(default=10.0, gt=0)
    timeout_s: float = Field(default=3.0, gt=0)
    unhealthy_threshold: int = Field(default=3, gt=0)

    @model_validator(mode="after")
    def _timeout_within_interval(self) -> HealthCheckConfig:
        """타임아웃이 주기보다 길면 확인이 밀려 상태 판정이 늦어진다."""
        if self.timeout_s > self.interval_s:
            raise ValueError("health check timeout_s must not exceed interval_s")
        return self


class RuntimeConfig(BaseModel):
    """Agent runtime settings."""

    model_config = ConfigDict(frozen=True)

    backend: Literal["docker"] = "docker"
    network: str = "malkuth-net"
    agent_base_image: str = "malkuth/agent-base:0.1.0"
    default_resources: ResourceDefaults = Field(default_factory=ResourceDefaults)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)

    @model_validator(mode="after")
    def _reject_host_network(self) -> RuntimeConfig:
        """호스트 네트워크 모드는 격리를 무력화한다 (02 Network)."""
        if self.network == "host":
            raise ValueError("agent containers must not use the 'host' network")
        return self


class ServiceDefaults(BaseModel):
    """Service run idle policy."""

    model_config = ConfigDict(frozen=True)

    idle_min_delay_s: float = Field(default=30.0, gt=0)
    idle_max_delay_s: float = Field(default=600.0, gt=0)
    max_failure_streak: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _min_below_max(self) -> ServiceDefaults:
        """min 이 max 보다 크면 backoff 가 진행되지 않는다."""
        if self.idle_min_delay_s > self.idle_max_delay_s:
            raise ValueError("idle_min_delay_s must not exceed idle_max_delay_s")
        return self


class OrchestratorConfig(BaseModel):
    """Graph orchestration settings."""

    model_config = ConfigDict(frozen=True)

    checkpointer: Literal["memory", "redis", "postgres"] = "memory"
    max_concurrent_runs: int = Field(default=10, gt=0)
    max_service_runs: int = Field(default=5, gt=0)
    node_timeout_s: float = Field(default=300.0, gt=0)
    service_defaults: ServiceDefaults = Field(default_factory=ServiceDefaults)


class A2AConfig(BaseModel):
    """A2A port allocation."""

    model_config = ConfigDict(frozen=True)

    port_range: tuple[int, int] = (9100, 9199)

    @model_validator(mode="after")
    def _valid_range(self) -> A2AConfig:
        """범위가 뒤집히면 포트를 하나도 할당할 수 없다."""
        low, high = self.port_range
        if low >= high:
            raise ValueError("a2a port_range must be ascending")
        if low < 1024:
            raise ValueError("a2a port_range must not use privileged ports")
        return self


class McpConfig(BaseModel):
    """MCP startup budget."""

    model_config = ConfigDict(frozen=True)

    startup_timeout_s: float = Field(default=15.0, gt=0)


class ProtocolsConfig(BaseModel):
    """Protocol layer settings."""

    model_config = ConfigDict(frozen=True)

    a2a: A2AConfig = Field(default_factory=A2AConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


class RegistryRoots(BaseModel):
    """Module resolution roots — every ref type must be covered."""

    model_config = ConfigDict(frozen=True)

    skillsets: str = "./modules/skillsets"
    promptsets: str = "./modules/promptsets"
    memorysets: str = "./modules/memorysets"
    agents: str = "./agents"
    graphs: str = "./graphs"


class RegistryConfig(BaseModel):
    """Module registry settings."""

    model_config = ConfigDict(frozen=True)

    backend: Literal["filesystem"] = "filesystem"
    roots: RegistryRoots = Field(default_factory=RegistryRoots)


class MemoryConfig(BaseModel):
    """Memory service settings."""

    model_config = ConfigDict(frozen=True)

    backend: Literal["sqlite", "postgres"] = "sqlite"
    index_lag_target_s: float = Field(default=5.0, gt=0)
    run_scope_retention_days: int = Field(default=30, gt=0)


class ObservabilityConfig(BaseModel):
    """Logging and metrics settings."""

    model_config = ConfigDict(frozen=True)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["pretty", "json"] = "json"
    metrics_port: int = Field(default=9090, gt=1023, lt=65536)


class MalkuthConfig(BaseModel):
    """The complete framework configuration.

    프레임워크 설정 전체. 모든 섹션이 기본값을 가지므로 빈 파일도 유효하다 —
    운영자가 바꾸고 싶은 것만 적는다.
    """

    model_config = ConfigDict(frozen=True)

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    protocols: ProtocolsConfig = Field(default_factory=ProtocolsConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


def _coerce(raw: str) -> Any:
    """환경변수 문자열을 YAML 스칼라로 해석한다 — 숫자/불리언이 문자열로 남지 않게."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collect ``MALKUTH_`` overrides into a nested mapping.

    ``MALKUTH_`` 환경변수를 중첩 매핑으로 모읍니다.
    ``MALKUTH_RUNTIME__NETWORK=x`` → ``{"runtime": {"network": "x"}}``.

    Args:
        environ: Source environment (defaults to the process environment).

    Returns:
        The nested override mapping.
    """
    source = os.environ if environ is None else environ
    overrides: dict[str, Any] = {}

    for key, value in source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].lower().split(NESTED_SEPARATOR)
        if not all(path):
            continue
        cursor = overrides
        for part in path[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        cursor[path[-1]] = _coerce(value)

    return overrides


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge an overlay into a base mapping.

    오버레이를 기반 매핑에 깊게 병합합니다 — 섹션 하나를 덮어써도 나머지
    키가 사라지지 않습니다.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge(current, value)
        else:
            merged[key] = value
    return merged


def load_config(
    environment: str = "dev",
    *,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    environ: Mapping[str, str] | None = None,
) -> MalkuthConfig:
    """Load and validate configuration for one environment.

    한 환경의 설정을 읽어 검증합니다. 파일 값 위에 ``MALKUTH_`` 환경변수가
    덮입니다 — 컨테이너 배포에서 파일을 다시 굽지 않고 조정할 수 있어야 합니다.

    Args:
        environment: Environment name (``dev`` / ``staging`` / ``prod``).
        config_dir: Directory holding ``{environment}.yaml``.
        environ: Source environment for overrides.

    Returns:
        The validated configuration.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the file is missing, unparseable,
            or fails schema validation — 잘못된 설정으로 기동하지 않습니다.
    """
    path = Path(config_dir) / f"{environment}.yaml"

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise config_error(
            "configuration file not found", path=str(path), environment=environment
        ) from err
    except (OSError, yaml.YAMLError) as err:
        raise config_error("configuration file could not be parsed", path=str(path)) from err

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise config_error("configuration root must be a mapping", path=str(path))

    merged = merge(raw, env_overrides(environ))

    try:
        return MalkuthConfig.model_validate(merged)
    except ValidationError as err:
        raise config_error(
            "configuration failed validation",
            path=str(path),
            errors=[
                {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]}
                for e in err.errors()
            ],
        ) from err


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "ENV_PREFIX",
    "NESTED_SEPARATOR",
    "A2AConfig",
    "HealthCheckConfig",
    "MalkuthConfig",
    "McpConfig",
    "MemoryConfig",
    "ObservabilityConfig",
    "OrchestratorConfig",
    "ProtocolsConfig",
    "RegistryConfig",
    "RegistryRoots",
    "ResourceDefaults",
    "RuntimeConfig",
    "ServiceDefaults",
    "config_error",
    "env_overrides",
    "load_config",
    "merge",
]
