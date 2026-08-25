"""Unit tests for manifest and group schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from malkuth.core.manifest import (
    RESERVED_GLOBAL_GROUP,
    AgentManifest,
    GroupManifest,
    ParsedModuleRef,
    ResourceSpec,
)
from tests.fixtures.builders import make_manifest, manifest_dict


def test_minimal_manifest_is_valid():
    manifest = make_manifest()

    assert manifest.name == "test-agent"
    assert manifest.spec.model.name == "claude-sonnet-5"


def test_manifest_is_frozen():
    manifest = make_manifest()

    with pytest.raises(ValidationError):
        manifest.metadata.name = "other"  # type: ignore[misc]


def test_group_defaults_to_global_when_undeclared():
    manifest = make_manifest()

    assert manifest.metadata.group is None
    assert manifest.group == RESERVED_GLOBAL_GROUP


def test_declaring_reserved_global_group_is_rejected():
    """`group: global` 직접 선언 금지 — 01-architecture.md Group Rules 1."""
    with pytest.raises(ValidationError, match="reserved"):
        make_manifest(metadata={"name": "a", "version": "0.1.0", "group": "global"})


def test_explicit_group_membership_is_accepted():
    manifest = make_manifest(metadata={"name": "a", "version": "0.1.0", "group": "research"})

    assert manifest.group == "research"


@pytest.mark.parametrize("name", ["Test-Agent", "test_agent", "테스트", "-agent", "agent-"])
def test_invalid_agent_names_are_rejected(name):
    with pytest.raises(ValidationError):
        make_manifest(metadata={"name": name, "version": "0.1.0"})


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc1", "latest"])
def test_non_semver_versions_are_rejected(version):
    with pytest.raises(ValidationError):
        make_manifest(metadata={"name": "a", "version": version})


@pytest.mark.parametrize(
    "ref",
    [
        "promptsets/test@latest",
        "promptsets/test",
        "test@0.1.0",
        "unknown/test@0.1.0",
        "skillsets/test@0.1.0",  # promptset 필드에 skillset ref
    ],
)
def test_invalid_promptset_refs_are_rejected(ref):
    with pytest.raises(ValidationError):
        make_manifest(
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": ref},
            }
        )


def test_skillset_field_rejects_non_skillset_ref():
    with pytest.raises(ValidationError, match="skillset ref"):
        make_manifest(
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
                "skillsets": [{"ref": "promptsets/other@0.1.0"}],
            }
        )


def _spec_with(**extra):
    return {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": "promptsets/test@0.1.0"},
        **extra,
    }


def test_stdio_mcp_server_requires_command():
    with pytest.raises(ValidationError, match="requires 'command'"):
        make_manifest(spec=_spec_with(mcp={"servers": [{"name": "fs", "transport": "stdio"}]}))


def test_stdio_mcp_server_rejects_shell_command():
    """셸 문자열 실행 금지 — 03-protocol-integration.md MCP Rules 5."""
    with pytest.raises(ValidationError, match="installed executable"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "fs",
                            "transport": "stdio",
                            "command": ["sh", "-c", "mcp-server-filesystem /workspace"],
                        }
                    ]
                }
            )
        )


def test_stdio_mcp_server_accepts_executable():
    manifest = make_manifest(
        spec=_spec_with(
            mcp={
                "servers": [
                    {
                        "name": "filesystem",
                        "transport": "stdio",
                        "command": ["mcp-server-filesystem", "/workspace"],
                        "allowed_tools": ["read_file", "list_directory"],
                    }
                ]
            }
        )
    )

    server = manifest.spec.mcp.servers[0]
    assert server.allowed_tools == ("read_file", "list_directory")
    assert server.optional is False


def test_http_mcp_server_requires_exactly_one_of_sidecar_or_url():
    with pytest.raises(ValidationError, match="exactly one"):
        make_manifest(
            spec=_spec_with(mcp={"servers": [{"name": "browser", "transport": "streamable-http"}]})
        )


def test_http_mcp_server_rejects_both_sidecar_and_url():
    with pytest.raises(ValidationError, match="exactly one"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "browser",
                            "transport": "streamable-http",
                            "sidecar": {"image": "mcp/playwright:1.2.0"},
                            "url": "https://example.com/mcp",
                        }
                    ]
                }
            )
        )


def test_sidecar_image_must_be_pinned():
    with pytest.raises(ValidationError, match="pinned"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "browser",
                            "transport": "streamable-http",
                            "sidecar": {"image": "mcp/playwright:latest"},
                        }
                    ]
                }
            )
        )


def test_external_mcp_server_auth_env_must_be_allowlisted():
    with pytest.raises(ValidationError, match="env_allowlist"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "corp-search",
                            "transport": "streamable-http",
                            "url": "https://mcp.internal.example.com/search",
                            "auth": {"type": "bearer", "token_env": "CORP_SEARCH_TOKEN"},
                        }
                    ]
                }
            )
        )


def test_external_mcp_server_auth_env_allowlisted_is_valid():
    manifest = make_manifest(
        spec=_spec_with(
            mcp={
                "servers": [
                    {
                        "name": "corp-search",
                        "transport": "streamable-http",
                        "url": "https://mcp.internal.example.com/search",
                        "auth": {"type": "bearer", "token_env": "CORP_SEARCH_TOKEN"},
                    }
                ]
            },
            runtime={"env_allowlist": ["CORP_SEARCH_TOKEN"]},
        )
    )

    assert manifest.spec.mcp.servers[0].auth is not None


def test_duplicate_mcp_server_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate mcp server name"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {"name": "fs", "transport": "stdio", "command": ["a"]},
                        {"name": "fs", "transport": "stdio", "command": ["b"]},
                    ]
                }
            )
        )


def test_mcp_env_allowlist_must_be_subset_of_runtime_allowlist():
    with pytest.raises(ValidationError, match="absent from"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "fs",
                            "transport": "stdio",
                            "command": ["mcp-server-filesystem"],
                            "env_allowlist": ["SECRET_KEY"],
                        }
                    ]
                }
            )
        )


def test_agent_image_must_be_pinned():
    with pytest.raises(ValidationError, match="pinned"):
        make_manifest(spec=_spec_with(runtime={"image": "malkuth/agent-base:latest"}))


def test_duplicate_memory_alias_is_rejected():
    with pytest.raises(ValidationError, match="duplicate memory space alias"):
        make_manifest(
            spec=_spec_with(
                memory={
                    "spaces": [
                        {"ref": "memorysets/a@0.1.0", "as": "mem"},
                        {"ref": "memorysets/b@0.1.0", "as": "mem"},
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "path", ["/var/run/docker.sock", "/etc/passwd", "/root/.ssh", "/proc/self"]
)
def test_sensitive_volume_mounts_are_rejected(path):
    with pytest.raises(ValidationError, match="sensitive host path"):
        make_manifest(spec=_spec_with(runtime={"volumes": [{"name": "v", "mount_path": path}]}))


@pytest.mark.parametrize("cpu", ["one", "1.0.0", "-1", ""])
def test_invalid_cpu_is_rejected(cpu):
    with pytest.raises(ValidationError):
        ResourceSpec(cpu=cpu)


@pytest.mark.parametrize("memory", ["1GB", "1024", "1.5Gi", ""])
def test_invalid_memory_is_rejected(memory):
    with pytest.raises(ValidationError):
        ResourceSpec(memory=memory)


def test_resource_conversions():
    spec = ResourceSpec(cpu="2.5", memory="2Gi")

    assert spec.cpu_cores == 2.5
    assert spec.memory_bytes == 2 * 1024**3


def test_wrong_kind_is_rejected():
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(manifest_dict(kind="Graph"))


def test_wrong_api_version_is_rejected():
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(manifest_dict(apiVersion="malkuth/v2"))


def _group_dict(**overrides):
    base = {
        "apiVersion": "malkuth/v1",
        "kind": "Group",
        "metadata": {"name": "research", "version": "0.1.0"},
        "spec": {
            "quotas": {"cpu": "8.0", "memory": "16Gi", "max_agents": 10},
            "secrets": ["SEARCH_API_KEY"],
        },
    }
    base.update(overrides)
    return base


def test_group_manifest_is_valid():
    group = GroupManifest.model_validate(_group_dict())

    assert group.name == "research"
    assert group.is_global is False
    assert group.spec.quotas.cpu_cores == 8.0
    assert group.spec.quotas.memory_bytes == 16 * 1024**3


def test_global_group_is_recognized():
    group = GroupManifest.model_validate(
        _group_dict(metadata={"name": "global", "version": "0.1.0"})
    )

    assert group.is_global is True


def test_group_definition_must_not_declare_membership():
    with pytest.raises(ValidationError):
        GroupManifest.model_validate(
            _group_dict(metadata={"name": "research", "version": "0.1.0", "group": "other"})
        )


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("skillsets/web-search@0.2.0", ("skillsets", "web-search", "0.2.0")),
        ("agents/planner@1.0.0", ("agents", "planner", "1.0.0")),
        ("memorysets/agent-longterm@0.1.0", ("memorysets", "agent-longterm", "0.1.0")),
    ],
)
def test_parse_module_ref_valid(ref, expected):
    parsed = ParsedModuleRef.parse(ref)

    assert (parsed.type, parsed.name, parsed.version) == expected
    assert str(parsed) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "web-search",
        "skillsets/x@latest",
        "skillsets/@1.0.0",
        "skillsets/x@main",
        "unknown/x@1.0.0",
        "skillsets/X@1.0.0",
    ],
)
def test_parse_module_ref_invalid(ref):
    with pytest.raises(ValueError, match="invalid module ref"):
        ParsedModuleRef.parse(ref)
