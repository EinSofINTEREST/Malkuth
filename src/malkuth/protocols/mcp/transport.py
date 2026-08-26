"""MCP transport patterns — stdio, sidecar, external.

전송 3 패턴. 격리 경계는 컨테이너 경계다 — stdio 서버는 소유 에이전트 컨테이너
내부에서만 돌고, sidecar 는 그 에이전트 전용이며, external 은 자격증명이
에이전트별로 분리된다 (03 Placement).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from malkuth.core.manifest import McpTransport
from malkuth.protocols.mcp.errors import startup_failed
from malkuth.protocols.mcp.session import Connection, ToolResult

if TYPE_CHECKING:
    from malkuth.core.manifest import McpServerSpec

log = structlog.get_logger(__name__)


def resolve_env(spec: McpServerSpec, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child process environment for a server.

    서버 프로세스에 전달할 환경을 만듭니다. ``env_allowlist`` 에 선언된 키만
    통과시킵니다 — 선언되지 않은 자격증명이 서버로 새지 않게 합니다
    (03 Security 5).

    Args:
        spec: The server declaration.
        environ: Source environment (defaults to the process environment).

    Returns:
        The filtered environment mapping.
    """
    source = os.environ if environ is None else environ
    return {key: source[key] for key in spec.env_allowlist if key in source}


@runtime_checkable
class StdioClient(Protocol):
    """Spawns and drives an MCP server child process.

    stdio 서버 프로세스를 띄우고 다루는 계약. 실제 SDK 는 이 뒤에 감춰지고
    테스트는 fake 로 대체한다.
    """

    async def spawn(self, *, command: list[str], env: dict[str, str]) -> Connection:
        """자식 프로세스를 띄우고 initialize 한다."""
        ...

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """tool 을 실행한다."""
        ...

    async def terminate(self, connection: Connection) -> None:
        """세션 종료 후 자식 프로세스를 회수한다."""
        ...


@runtime_checkable
class HttpClient(Protocol):
    """Connects to an HTTP-family MCP server.

    HTTP 계열 서버 접속 계약.
    """

    async def connect(self, *, url: str, headers: dict[str, str]) -> Connection:
        """원격 서버에 접속해 initialize 한다."""
        ...

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """tool 을 실행한다."""
        ...

    async def disconnect(self, connection: Connection) -> None:
        """연결을 정리한다."""
        ...


@dataclass
class StdioTransport:
    """Runs an MCP server as a child process inside the agent container.

    소유 에이전트 컨테이너 안에서 MCP 서버를 자식 프로세스로 실행합니다.
    ``command`` 는 이미지에 설치된 실행 파일만 — 셸 문자열은 manifest 검증에서
    이미 차단됩니다.
    """

    agent: str
    client: StdioClient
    environ: Mapping[str, str] | None = None

    async def connect(self, spec: McpServerSpec) -> Connection:
        """자식 프로세스를 띄우고 initialize 한다."""
        if spec.transport is not McpTransport.STDIO:
            raise startup_failed(self.agent, spec.name, reason="transport mismatch")
        return await self.client.spawn(
            command=list(spec.command),
            env=resolve_env(spec, self.environ),
        )

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """자식 프로세스의 tool 을 호출한다."""
        return await self.client.call(connection, tool, arguments)

    async def close(self, connection: Connection) -> None:
        """세션 종료 후 자식 프로세스를 회수한다 — 좀비 금지."""
        await self.client.terminate(connection)


@dataclass
class HttpTransport:
    """Connects to an HTTP-family MCP server.

    HTTP 계열 MCP 서버에 접속합니다 — sidecar(전용 컨테이너, URL 은 runtime 주입)
    와 external(명시 URL + auth) 두 패턴을 함께 다룹니다.
    """

    agent: str
    client: HttpClient
    sidecar_urls: dict[str, str] = field(default_factory=dict)
    """runtime 이 사이드카 기동 후 주입한 URL — manifest 에 수동 기입 금지."""

    environ: Mapping[str, str] | None = None

    def url_for(self, spec: McpServerSpec) -> str:
        """서버의 접속 URL — sidecar 는 주입값, external 은 선언값."""
        if spec.sidecar is not None:
            url = self.sidecar_urls.get(spec.name)
            if url is None:
                raise startup_failed(self.agent, spec.name, reason="sidecar url not injected")
            return url
        if spec.url is None:
            raise startup_failed(self.agent, spec.name, reason="missing url")
        return spec.url

    def headers_for(self, spec: McpServerSpec) -> dict[str, str]:
        """인증 헤더 — 토큰 값은 env 에서 읽고 절대 로그로 남기지 않는다."""
        if spec.auth is None:
            return {}
        source = os.environ if self.environ is None else self.environ
        token = source.get(spec.auth.token_env)
        if not token:
            raise startup_failed(
                self.agent,
                spec.name,
                reason="auth token unavailable",
                token_env=spec.auth.token_env,
            )
        return {"authorization": f"Bearer {token}"}

    async def connect(self, spec: McpServerSpec) -> Connection:
        """원격 서버에 접속해 initialize 한다."""
        if spec.transport is McpTransport.STDIO:
            raise startup_failed(self.agent, spec.name, reason="transport mismatch")
        return await self.client.connect(url=self.url_for(spec), headers=self.headers_for(spec))

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """원격 tool 을 호출한다."""
        return await self.client.call(connection, tool, arguments)

    async def close(self, connection: Connection) -> None:
        """연결을 정리한다."""
        await self.client.disconnect(connection)


@dataclass
class TransportSelector:
    """Picks the transport implementation for a server declaration.

    서버 선언에 맞는 전송 구현을 고릅니다. 라우터가 아니라 **선택기**입니다 —
    세션이 자기 전송을 직접 들고 있어야 호출/정리가 같은 연결을 가리킵니다.
    """

    stdio: StdioTransport
    http: HttpTransport

    def for_spec(self, spec: McpServerSpec) -> StdioTransport | HttpTransport:
        """선언된 transport 에 맞는 구현 — stdio 아니면 HTTP 계열."""
        if spec.transport is McpTransport.STDIO:
            return self.stdio
        return self.http


__all__ = [
    "HttpClient",
    "HttpTransport",
    "StdioClient",
    "StdioTransport",
    "TransportSelector",
    "resolve_env",
]
