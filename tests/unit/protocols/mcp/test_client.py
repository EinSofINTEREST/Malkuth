"""Unit tests for the per-agent MCP client and transports.

네임스페이싱, env 격리, sidecar URL 주입, 세션 소유 관계를 검증한다.
"""

from __future__ import annotations

import pytest

from malkuth.core.agent import HealthState
from malkuth.core.errors import MalkuthError
from malkuth.core.manifest import McpServerSpec
from malkuth.protocols.mcp.client import McpClient, split_namespaced
from malkuth.protocols.mcp.transport import (
    HttpTransport,
    StdioTransport,
    TransportSelector,
    resolve_env,
)
from tests.fixtures.fake_mcp import FakeHttpClient, FakeStdioClient


def spec(**overrides) -> McpServerSpec:
    base = {"name": "filesystem", "transport": "stdio", "command": ["mcp-server-fs"]}
    base.update(overrides)
    return McpServerSpec.model_validate(base)


def make_client(
    stdio: FakeStdioClient | None = None,
    http: FakeHttpClient | None = None,
    *,
    sidecar_urls: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> McpClient:
    selector = TransportSelector(
        stdio=StdioTransport(
            agent="researcher", client=stdio or FakeStdioClient(), environ=environ or {}
        ),
        http=HttpTransport(
            agent="researcher",
            client=http or FakeHttpClient(),
            sidecar_urls=sidecar_urls or {},
            environ=environ or {},
        ),
    )
    return McpClient(agent="researcher", transports=selector)


# --- 네임스페이싱 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__fs__read_file", ("fs", "read_file")),
        ("mcp__fs__read__file", ("fs", "read__file")),
        ("read_file", None),
        ("mcp__fs", None),
        ("mcp____read", None),
    ],
)
def test_split_namespaced(name, expected):
    """skillset tool 과 MCP tool 을 이름만으로 구분할 수 있어야 한다."""
    assert split_namespaced(name) == expected


# --- env 격리 ----------------------------------------------------------------


def test_env_allowlist_filters_what_reaches_the_server():
    """선언되지 않은 자격증명이 서버 프로세스로 새면 안 된다 (03 Security 5)."""
    declared = spec(env_allowlist=["SEARCH_API_KEY"])

    env = resolve_env(declared, {"SEARCH_API_KEY": "k", "ANTHROPIC_API_KEY": "secret"})

    assert env == {"SEARCH_API_KEY": "k"}


def test_absent_declared_key_is_simply_omitted():
    declared = spec(env_allowlist=["MISSING"])

    assert resolve_env(declared, {}) == {}


# --- stdio -------------------------------------------------------------------


async def test_stdio_start_spawns_with_filtered_env():
    stdio = FakeStdioClient(["read_file"])
    client = make_client(stdio, environ={"TOKEN": "t", "OTHER": "o"})

    tools = await client.start(spec(env_allowlist=["TOKEN"]))

    assert tools == ("read_file",)
    command, env = stdio.spawned[0]
    assert command == ["mcp-server-fs"]
    assert env == {"TOKEN": "t"}


async def test_shutdown_terminates_child_processes():
    """좀비 프로세스를 남기지 않는다."""
    stdio = FakeStdioClient(["read_file"])
    client = make_client(stdio)
    await client.start(spec())

    await client.shutdown()

    assert stdio.terminated == 1
    assert client.sessions == {}


# --- sidecar / external ------------------------------------------------------


async def test_sidecar_url_is_injected_by_the_runtime():
    """사이드카 URL 은 manifest 에 수동 기입하지 않는다."""
    http = FakeHttpClient(["screenshot"])
    client = make_client(http=http, sidecar_urls={"browser": "http://sidecar:9000"})
    declared = McpServerSpec.model_validate(
        {
            "name": "browser",
            "transport": "streamable-http",
            "sidecar": {"image": "mcp/playwright:1.2.0"},
        }
    )

    await client.start(declared)

    url, headers = http.connections[0]
    assert url == "http://sidecar:9000"
    assert headers == {}


async def test_missing_sidecar_url_fails_startup():
    client = make_client(sidecar_urls={})
    declared = McpServerSpec.model_validate(
        {
            "name": "browser",
            "transport": "streamable-http",
            "sidecar": {"image": "mcp/playwright:1.2.0"},
        }
    )

    with pytest.raises(MalkuthError) as exc_info:
        await client.start(declared)

    assert exc_info.value.code == "MCP_001"


async def test_external_server_sends_the_auth_header():
    http = FakeHttpClient(["search"])
    client = make_client(http=http, environ={"CORP_TOKEN": "s3cret"})
    declared = McpServerSpec.model_validate(
        {
            "name": "corp-search",
            "transport": "streamable-http",
            "url": "https://mcp.example.com",
            "auth": {"type": "bearer", "token_env": "CORP_TOKEN"},
        }
    )

    await client.start(declared)

    _url, headers = http.connections[0]
    assert headers == {"authorization": "Bearer s3cret"}


async def test_missing_auth_token_fails_startup():
    """토큰이 없으면 무인증으로 조용히 붙지 않는다."""
    client = make_client(environ={})
    declared = McpServerSpec.model_validate(
        {
            "name": "corp-search",
            "transport": "streamable-http",
            "url": "https://mcp.example.com",
            "auth": {"type": "bearer", "token_env": "CORP_TOKEN"},
        }
    )

    with pytest.raises(MalkuthError) as exc_info:
        await client.start(declared)

    assert exc_info.value.code == "MCP_001"
    assert "s3cret" not in str(exc_info.value)


# --- tool 라우팅 --------------------------------------------------------------


async def test_call_tool_routes_to_the_owning_session():
    client = make_client(FakeStdioClient(["read_file"]))
    await client.start(spec())

    result = await client.call_tool("mcp__filesystem__read_file", {"path": "a"})

    assert result.content == "read_file"


async def test_unknown_server_is_mcp_002():
    client = make_client()

    with pytest.raises(MalkuthError) as exc_info:
        await client.call_tool("mcp__absent__read_file", {})

    assert exc_info.value.code == "MCP_002"


async def test_unnamespaced_name_is_mcp_002():
    """skillset tool 이름이 MCP 클라이언트로 새어 들어오면 안 된다."""
    client = make_client()

    with pytest.raises(MalkuthError) as exc_info:
        await client.call_tool("read_file", {})

    assert exc_info.value.code == "MCP_002"


async def test_tools_reports_namespaced_names():
    client = make_client(FakeStdioClient(["read_file", "list_directory"]))
    await client.start(spec())

    assert set(client.tools()) == {
        "mcp__filesystem__read_file",
        "mcp__filesystem__list_directory",
    }


async def test_health_reports_each_session():
    client = make_client(FakeStdioClient(["read_file"]))
    await client.start(spec())

    health = client.health()

    assert health["mcp:filesystem"].state is HealthState.HEALTHY


def test_selector_picks_transport_by_declaration():
    client = make_client()
    stdio_spec = spec()
    http_spec = McpServerSpec.model_validate(
        {"name": "browser", "transport": "streamable-http", "url": "https://x"}
    )

    assert isinstance(client.transports.for_spec(stdio_spec), StdioTransport)
    assert isinstance(client.transports.for_spec(http_spec), HttpTransport)


# --- agentd 결합 --------------------------------------------------------------


async def test_client_satisfies_the_bootstrap_launcher_contract():
    """bootstrap 이 이 클라이언트를 그대로 물 수 있어야 한다 — #45 의 존재 이유."""
    from malkuth.agentd.bootstrap import Bootstrap, McpLauncher
    from tests.fixtures.builders import make_manifest

    client = make_client(FakeStdioClient(["read_file"]))
    assert isinstance(client, McpLauncher)

    manifest = make_manifest(
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "mcp": {
                "servers": [
                    {"name": "filesystem", "transport": "stdio", "command": ["mcp-server-fs"]}
                ]
            },
        }
    )

    class Loader:
        def load(self, ref: str):
            class Empty:
                ref = "skillsets/empty@0.1.0"

                def tools(self):
                    return ()

            return Empty()

    result = await Bootstrap(
        manifest,
        promptset_loader=Loader(),
        skillset_loader=Loader(),
        mcp_launcher=client,
    ).run()

    assert set(result.tools) == {"mcp__filesystem__read_file"}
