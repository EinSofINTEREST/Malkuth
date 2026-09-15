"""The MCP client this agent's declarations call for (#282).

선언된 MCP 서버의 세션을 소유할 클라이언트를 조립한다. 기동과 E2E 가 같은 조립을 탄다 — 두 벌이면
검증한 경로와 실제 경로가 갈린다.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from malkuth.protocols.mcp.client import McpClient
from malkuth.protocols.mcp.sdk import SdkHttpClient, SdkStdioClient
from malkuth.protocols.mcp.transport import HttpTransport, StdioTransport, TransportSelector

if TYPE_CHECKING:
    from collections.abc import Mapping

    from malkuth.core.manifest import AgentManifest
    from malkuth.observability.metrics import Metrics


def build_mcp_client(
    manifest: AgentManifest,
    *,
    metrics: Metrics | None = None,
    environ: Mapping[str, str] | None = None,
) -> McpClient | None:
    """The client owning this agent's MCP sessions — None when nothing is declared."""
    if not manifest.spec.mcp.servers:
        return None
    env = os.environ if environ is None else environ
    agent = manifest.name
    return McpClient(
        agent=agent,
        transports=TransportSelector(
            stdio=StdioTransport(agent=agent, client=SdkStdioClient(), environ=env),
            http=HttpTransport(agent=agent, client=SdkHttpClient(), environ=env),
        ),
        metrics=metrics,
    )


__all__ = ["build_mcp_client"]
