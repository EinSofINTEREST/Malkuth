"""Agent manifest and group schemas.

에이전트 계약 선언(manifest)과 그룹(리소스 스코프 경계) 스키마.
Manifest 는 에이전트의 유일한 계약 소스이며, 코드에 하드코딩된
모델명/프롬프트/tool 목록은 금지된다.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESERVED_GLOBAL_GROUP = "global"

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_MODULE_REF_PATTERN = re.compile(
    r"^(?P<type>skillsets|promptsets|memorysets|agents|graphs)/"
    r"(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)@(?P<version>\d+\.\d+\.\d+)$"
)
_RESOURCE_CPU_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")
_SHELL_BASENAMES = frozenset(
    {"sh", "bash", "zsh", "dash", "ash", "ksh", "fish", "csh", "tcsh", "env"}
)
_RESOURCE_MEMORY_PATTERN = re.compile(r"^\d+(?:Ki|Mi|Gi|Ti)$")

ModuleRefStr = Annotated[str, Field(pattern=_MODULE_REF_PATTERN.pattern)]
"""모듈 참조 문자열 — ``{type}/{name}@{version}``. ``latest`` 등 비고정 참조 금지."""

AgentName = Annotated[str, Field(pattern=_NAME_PATTERN.pattern)]
SemVer = Annotated[str, Field(pattern=_SEMVER_PATTERN.pattern)]


def _require_pinned_image(value: str, subject: str) -> str:
    """Reject images that are not pinned to an explicit tag or digest.

    이미지가 태그 또는 digest 로 고정되었는지 검사합니다.

    태그 판정은 **마지막 경로 세그먼트**에서만 수행합니다 — ``registry.local:5000/x``
    처럼 레지스트리 포트가 있는 참조를 태그로 오인하면, 태그 없는 이미지가
    검증을 통과해 pull 시점에 ``latest`` 로 해석됩니다.

    Args:
        value: The image reference.
        subject: Subject name used in the error message (e.g. ``"agent"``).

    Returns:
        The validated image reference.

    Raises:
        ValueError: If the reference is not pinned.
    """
    if "@" in value:  # digest 고정 (예: image@sha256:...)
        return value

    last_segment = value.rsplit("/", 1)[-1]
    tag = last_segment.partition(":")[2]
    if not tag or tag == "latest":
        raise ValueError(
            f"{subject} image must be pinned to an explicit tag or digest (no 'latest')"
        )
    return value


class McpTransport(StrEnum):
    """MCP transport 종류."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"


class MemoryMode(StrEnum):
    """Memory space 접근 권한."""

    RW = "rw"
    RO = "ro"


class Metadata(BaseModel):
    """Agent manifest metadata.

    에이전트 식별 정보. ``group`` 은 최대 하나이며 예약 그룹 ``global`` 을
    직접 선언하는 것은 금지된다 (모든 에이전트는 암묵적 global 멤버).
    """

    model_config = ConfigDict(frozen=True)

    name: AgentName
    version: SemVer
    group: str | None = None
    description: str | None = None

    @field_validator("group")
    @classmethod
    def _reject_reserved_group(cls, value: str | None) -> str | None:
        """예약 그룹 직접 선언 차단 — 배포 검증에서 ``CFG_002`` 로 이어진다."""
        if value is None:
            return None
        if value == RESERVED_GLOBAL_GROUP:
            raise ValueError(
                "group 'global' is reserved — omit metadata.group instead of declaring it"
            )
        if not _NAME_PATTERN.match(value):
            raise ValueError("group must be lowercase alphanumeric with hyphens")
        return value


class ModelConfig(BaseModel):
    """Model provider configuration.

    모델 설정 — 코드가 아니라 manifest 가 모델을 결정한다.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    name: str
    max_tokens: int | None = None
    temperature: float | None = None


class ModuleRef(BaseModel):
    """A versioned module reference.

    버전 고정된 모듈 참조.
    """

    model_config = ConfigDict(frozen=True)

    ref: ModuleRefStr


class MemorySpaceRef(BaseModel):
    """A memory space attachment.

    Memory space 부착 선언. ``as`` 별칭은 에이전트 관점의 논리 이름이며,
    충돌 시 해석 순서는 local > group > global 이다.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    ref: ModuleRefStr
    alias: str = Field(alias="as")
    mode: MemoryMode = MemoryMode.RW
    writers: tuple[AgentName, ...] = ()


class MemorySpec(BaseModel):
    """Declared memory spaces.

    선언된 memory space 목록 — 미선언 space 접근은 ``MEM_001`` 로 거부된다.
    """

    model_config = ConfigDict(frozen=True)

    spaces: tuple[MemorySpaceRef, ...] = ()

    @field_validator("spaces")
    @classmethod
    def _unique_aliases(cls, value: tuple[MemorySpaceRef, ...]) -> tuple[MemorySpaceRef, ...]:
        """같은 선언 위치 안에서 별칭 중복 금지."""
        aliases = [s.alias for s in value]
        duplicates = {a for a in aliases if aliases.count(a) > 1}
        if duplicates:
            raise ValueError(f"duplicate memory space alias: {sorted(duplicates)}")
        return value


class McpAuth(BaseModel):
    """External MCP server authentication.

    외부 MCP 서버 인증 — 토큰 값이 아니라 env 키만 선언한다.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["bearer"]
    token_env: str


class McpSidecar(BaseModel):
    """Sidecar container spec for an agent-owned MCP server.

    에이전트 전용 MCP 사이드카. 소유 에이전트와 1:1 이며 lifecycle 을 함께한다.
    """

    model_config = ConfigDict(frozen=True)

    image: str
    resources: ResourceSpec | None = None

    @field_validator("image")
    @classmethod
    def _reject_latest_tag(cls, value: str) -> str:
        """사이드카 이미지는 semver 태그 고정 — ``latest`` 금지."""
        return _require_pinned_image(value, "sidecar")


class McpServerSpec(BaseModel):
    """An MCP server owned by exactly one agent.

    MCP 서버 선언 — stdio / sidecar / external 3 패턴.
    프로토콜 자원은 정확히 하나의 에이전트에 속한다.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    transport: McpTransport
    command: tuple[str, ...] = ()
    sidecar: McpSidecar | None = None
    url: str | None = None
    auth: McpAuth | None = None
    allowed_tools: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = ()
    optional: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        """서버 이름은 tool 네임스페이스(``mcp__{server}__{tool}``)에 쓰인다."""
        if not _NAME_PATTERN.match(value):
            raise ValueError("mcp server name must be lowercase alphanumeric with hyphens")
        return value

    @model_validator(mode="after")
    def _check_transport_shape(self) -> McpServerSpec:
        """전송 방식별 필수 필드와 보안 규칙을 검증한다."""
        if self.transport is McpTransport.STDIO:
            if not self.command:
                raise ValueError("stdio transport requires 'command'")
            if self.sidecar is not None or self.url is not None:
                raise ValueError("stdio transport must not declare 'sidecar' or 'url'")
            # 셸 문자열 실행 금지 — 이미지에 설치된 실행 파일만 허용
            if PurePosixPath(self.command[0]).name in _SHELL_BASENAMES:
                raise ValueError("stdio command must be an installed executable, not a shell")
            return self

        # HTTP 계열 — sidecar 와 external 중 정확히 하나
        if self.command:
            raise ValueError("'command' is only valid for stdio transport")
        if (self.sidecar is None) == (self.url is None):
            raise ValueError("http transport requires exactly one of 'sidecar' or 'url'")
        if self.sidecar is not None and self.auth is not None:
            raise ValueError("sidecar servers must not declare 'auth' — url is runtime-injected")
        return self


class McpSpec(BaseModel):
    """Per-agent MCP server declarations.

    이 에이전트 전용 MCP 서버 목록 — 서버 이름 중복은 금지된다.
    """

    model_config = ConfigDict(frozen=True)

    servers: tuple[McpServerSpec, ...] = ()

    @field_validator("servers")
    @classmethod
    def _unique_names(cls, value: tuple[McpServerSpec, ...]) -> tuple[McpServerSpec, ...]:
        """동일 서버 이름 중복 선언 차단 — tool 네임스페이스 충돌 방지."""
        names = [s.name for s in value]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate mcp server name: {sorted(duplicates)}")
        return value


class A2ACapabilities(BaseModel):
    """A2A capability flags advertised on the AgentCard."""

    model_config = ConfigDict(frozen=True)

    streaming: bool = False
    push_notifications: bool = False


class A2ASpec(BaseModel):
    """A2A exposure settings.

    A2A 노출 설정 — peer 호출을 받으려면 ``enabled: true`` 가 필요하다.
    포트는 runtime 이 할당하므로 선언하지 않는다.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    capabilities: A2ACapabilities = Field(default_factory=A2ACapabilities)


class ResourceSpec(BaseModel):
    """CPU/memory limits.

    리소스 상한 — 그룹 quota 합계 검증의 단위가 된다.
    """

    model_config = ConfigDict(frozen=True)

    cpu: str = "1.0"
    memory: str = "1Gi"

    @field_validator("cpu")
    @classmethod
    def _valid_cpu(cls, value: str) -> str:
        """CPU 는 코어 수 문자열 (예: ``"1.0"``)."""
        if not _RESOURCE_CPU_PATTERN.match(value):
            raise ValueError("cpu must be a decimal core count string, e.g. '1.0'")
        return value

    @field_validator("memory")
    @classmethod
    def _valid_memory(cls, value: str) -> str:
        """Memory 는 Ki/Mi/Gi/Ti 접미사 (예: ``"1Gi"``)."""
        if not _RESOURCE_MEMORY_PATTERN.match(value):
            raise ValueError("memory must be an integer with Ki/Mi/Gi/Ti suffix, e.g. '1Gi'")
        return value

    @property
    def cpu_cores(self) -> float:
        """코어 수 — quota 합산에 사용."""
        return float(self.cpu)

    @property
    def memory_bytes(self) -> int:
        """바이트 단위 메모리 — quota 합산에 사용."""
        units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
        suffix = self.memory[-2:]
        return int(self.memory[:-2]) * units[suffix]


class OutputSpec(BaseModel):
    """The named output keys an agent produces.

    선언형 에이전트가 이름 있는 출력을 만들 수 있게 하는 계약. 미선언 시
    실행기는 ``{"content": ...}`` 하나만 돌려주며, 그러면 그래프의
    ``output_map`` 이 ``output.content`` 밖을 가리킬 수 없다.

    **지시는 promptset 이 한다** — 여기서는 무엇이 와야 하는지만 선언하고,
    실행기는 그 선언과 실제 응답이 어긋나면 실패시킨다 (04 는 프롬프트를
    모듈에 두라고 규정하므로 실행기가 지시문을 덧붙이지 않는다).
    """

    model_config = ConfigDict(frozen=True)

    keys: tuple[str, ...] = ()
    """응답에서 뽑아 ``TaskResult.output`` 으로 옮길 키."""

    @field_validator("keys")
    @classmethod
    def _named_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """키는 state schema 필드명과 맞물리므로 식별자 모양이어야 한다."""
        for key in value:
            if not key.isidentifier():
                raise ValueError(f"output key must be an identifier: {key!r}")
        if len(set(value)) != len(value):
            raise ValueError("output keys must be unique")
        return value


class VolumeSpec(BaseModel):
    """An explicitly declared volume mount.

    명시 선언된 볼륨 — 에이전트 간 공유는 금지된다 (사이드채널 차단).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    mount_path: str
    read_only: bool = False

    @field_validator("mount_path")
    @classmethod
    def _reject_sensitive_paths(cls, value: str) -> str:
        """호스트 민감 경로 마운트 절대 금지."""
        forbidden = ("/var/run/docker.sock", "/etc", "/root", "/proc", "/sys")
        if any(value == p or value.startswith(f"{p}/") for p in forbidden):
            raise ValueError(f"mounting sensitive host path is forbidden: {value}")
        return value


class RuntimeSpec(BaseModel):
    """Container runtime settings.

    컨테이너 실행 설정. ``env_allowlist`` 의 각 키는 배포 검증에서
    local > group > global 스코프 체인으로 해석 가능해야 한다.
    """

    model_config = ConfigDict(frozen=True)

    image: str | None = None
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    env_allowlist: tuple[str, ...] = ()
    volumes: tuple[VolumeSpec, ...] = ()
    replicas: int = 1
    max_concurrent_tasks: int = 4

    @field_validator("image")
    @classmethod
    def _reject_latest_tag(cls, value: str | None) -> str | None:
        """이미지 태그는 semver — ``latest`` 배포 금지."""
        if value is None:
            return None
        return _require_pinned_image(value, "agent")


class AgentSpec(BaseModel):
    """The agent contract body.

    에이전트 계약 본문 — 선언되지 않은 모듈/서버/자원의 사용은 금지된다.
    """

    model_config = ConfigDict(frozen=True)

    model: ModelConfig
    promptset: ModuleRef
    skillsets: tuple[ModuleRef, ...] = ()
    memory: MemorySpec = Field(default_factory=MemorySpec)
    mcp: McpSpec = Field(default_factory=McpSpec)
    a2a: A2ASpec = Field(default_factory=A2ASpec)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    entrypoint: str | None = None
    output: OutputSpec = Field(default_factory=OutputSpec)

    @field_validator("promptset")
    @classmethod
    def _promptset_type(cls, value: ModuleRef) -> ModuleRef:
        """promptset 필드는 promptsets 타입 ref 만 받는다."""
        if not value.ref.startswith("promptsets/"):
            raise ValueError("promptset ref must be of type 'promptsets'")
        return value

    @field_validator("skillsets")
    @classmethod
    def _skillset_types(cls, value: tuple[ModuleRef, ...]) -> tuple[ModuleRef, ...]:
        """skillsets 필드는 skillsets 타입 ref 만 받는다."""
        for item in value:
            if not item.ref.startswith("skillsets/"):
                raise ValueError("skillset ref must be of type 'skillsets'")
        return value


class AgentManifest(BaseModel):
    """The single source of truth for an agent's contract.

    에이전트의 유일한 계약 소스. 배포 시 이 스키마로 검증되며,
    미검증 manifest 로는 컨테이너를 기동하지 않는다.
    """

    model_config = ConfigDict(frozen=True)

    api_version: Literal["malkuth/v1"] = Field(alias="apiVersion")
    kind: Literal["Agent"]
    metadata: Metadata
    spec: AgentSpec

    @model_validator(mode="after")
    def _check_mcp_env_declared(self) -> AgentManifest:
        """MCP 서버가 요구하는 env 키가 에이전트 allowlist 에 있는지 확인한다."""
        allowed = set(self.spec.runtime.env_allowlist)
        for server in self.spec.mcp.servers:
            missing = set(server.env_allowlist) - allowed
            if missing:
                raise ValueError(
                    f"mcp server '{server.name}' requires env keys absent from "
                    f"runtime.env_allowlist: {sorted(missing)}"
                )
            if server.auth is not None and server.auth.token_env not in allowed:
                raise ValueError(
                    f"mcp server '{server.name}' auth token_env "
                    f"'{server.auth.token_env}' must be in runtime.env_allowlist"
                )
        return self

    @property
    def name(self) -> str:
        """에이전트 이름 — 그래프가 참조하는 id."""
        return self.metadata.name

    @property
    def group(self) -> str:
        """소속 그룹 — 미선언 시 예약 그룹 ``global``."""
        return self.metadata.group or RESERVED_GLOBAL_GROUP


class GroupQuotas(BaseModel):
    """Aggregate resource ceiling for group members.

    소속 에이전트 리소스 합계 상한 — 초과 시 기동 거부(``RT_006``).
    """

    model_config = ConfigDict(frozen=True)

    cpu: str | None = None
    memory: str | None = None
    max_agents: int | None = None

    @field_validator("cpu")
    @classmethod
    def _valid_cpu(cls, value: str | None) -> str | None:
        """quota 오타를 집계 시점이 아니라 배포 검증에서 잡는다."""
        if value is not None and not _RESOURCE_CPU_PATTERN.match(value):
            raise ValueError("cpu must be a decimal core count string, e.g. '8.0'")
        return value

    @field_validator("memory")
    @classmethod
    def _valid_memory(cls, value: str | None) -> str | None:
        """quota 오타를 집계 시점이 아니라 배포 검증에서 잡는다."""
        if value is not None and not _RESOURCE_MEMORY_PATTERN.match(value):
            raise ValueError("memory must be an integer with Ki/Mi/Gi/Ti suffix, e.g. '16Gi'")
        return value

    @property
    def cpu_cores(self) -> float | None:
        """상한 코어 수."""
        return float(self.cpu) if self.cpu is not None else None

    @property
    def memory_bytes(self) -> int | None:
        """상한 메모리 바이트."""
        if self.memory is None:
            return None
        return ResourceSpec(memory=self.memory).memory_bytes


class GroupSpec(BaseModel):
    """Group-scoped resources.

    그룹 스코프 리소스 — 그룹은 리소스 경계일 뿐 우열이나 연결 권한이 아니다.
    """

    model_config = ConfigDict(frozen=True)

    quotas: GroupQuotas = Field(default_factory=GroupQuotas)
    secrets: tuple[str, ...] = ()
    memory: MemorySpec = Field(default_factory=MemorySpec)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class GroupManifest(BaseModel):
    """A group definition (``groups/<name>.yaml``).

    그룹 정의. 예약 그룹 ``global`` 은 전역 스코프 리소스 선언 전용이다.
    """

    model_config = ConfigDict(frozen=True)

    api_version: Literal["malkuth/v1"] = Field(alias="apiVersion")
    kind: Literal["Group"]
    metadata: Metadata
    spec: GroupSpec

    @model_validator(mode="after")
    def _check_group_metadata(self) -> GroupManifest:
        """그룹 정의의 metadata 는 자신의 이름만 갖는다 (소속 선언 금지)."""
        if self.metadata.group is not None:
            raise ValueError("group definitions must not declare metadata.group")
        return self

    @property
    def name(self) -> str:
        """그룹 이름."""
        return self.metadata.name

    @property
    def is_global(self) -> bool:
        """예약 전역 그룹인지."""
        return self.metadata.name == RESERVED_GLOBAL_GROUP


class ParsedModuleRef(BaseModel):
    """A parsed module reference.

    파싱된 모듈 참조 — registry 가 경로로 해석하는 입력.
    """

    model_config = ConfigDict(frozen=True)

    type: str
    name: str
    version: str

    @classmethod
    def parse(cls, ref: str) -> ParsedModuleRef:
        """Parse a ``{type}/{name}@{version}`` reference.

        모듈 참조 문자열을 파싱합니다. 형식 위반 시 ``ValueError`` —
        registry boundary 에서 ``MOD_001`` 로 변환됩니다.

        Args:
            ref: Module reference in ``{type}/{name}@{version}`` format.

        Returns:
            The parsed reference.

        Raises:
            ValueError: If the reference is malformed or not version-pinned.
        """
        match = _MODULE_REF_PATTERN.match(ref)
        if match is None:
            raise ValueError(f"invalid module ref: {ref}")
        return cls(
            type=match.group("type"),
            name=match.group("name"),
            version=match.group("version"),
        )

    def __str__(self) -> str:
        """정규 표현 문자열로 되돌린다."""
        return f"{self.type}/{self.name}@{self.version}"
