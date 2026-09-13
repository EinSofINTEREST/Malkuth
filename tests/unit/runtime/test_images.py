"""Baking an agent image from stored materials (#265).

조립이 맞는지, 규약 위반을 굽기 전에 잡는지, 그리고 **임시 디렉토리가 남지 않는지**.
마지막이 이 단계의 존재 이유다 — 작업 트리에 재료를 남기지 않으려고 만든 경로다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.materials import InMemoryMaterialStore, Materials
from malkuth.runtime.docker.client import ImageBuildError
from malkuth.runtime.images import (
    SKELETON_DOCKERFILE,
    BuildStatus,
    ImageBuilder,
    InMemoryBuildStore,
    check_dockerfile,
    image_tag,
)
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]
VALID_DOCKERFILE = "FROM malkuth/agent-base:0.1.0\nCOPY manifest.yaml /app/manifest.yaml\n"


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "malkuth/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}},
        },
    )
    write(
        tmp_path / "agents" / "custom" / "manifest.yaml",
        {
            "apiVersion": "malkuth/v1",
            "kind": "Agent",
            "metadata": {"name": "custom", "version": "0.1.0"},
            "spec": {
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/solo@0.1.0"},
            },
        },
    )
    for name in ("graphs", "groups"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def docker() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def builder(workspace, docker) -> ImageBuilder:
    materials = InMemoryMaterialStore()
    materials.put(
        Materials(agent="custom", version="0.1.0", files={"src/agent.py": "MARK = 'custom'"})
    )
    return ImageBuilder(
        catalog=Catalog.under(workspace),
        materials=materials,
        builds=InMemoryBuildStore(),
        client=docker,
        workspace=workspace / "work",
    )


# --- Dockerfile 규약 (#263) ----------------------------------------------------------


def test_a_dockerfile_from_the_base_is_accepted():
    check_dockerfile(VALID_DOCKERFILE)


def test_the_skeleton_satisfies_its_own_rules():
    """기본으로 쓰는 것이 규약을 어기면 재료 없는 에이전트가 전부 막힌다."""
    check_dockerfile(SKELETON_DOCKERFILE)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("COPY x /x\n", "FROM 이 없다"),
        ("FROM python:3.12-slim\n", "base 가 아니다 — agentd 가 없다"),
        ("FROM malkuth/agent-base:0.1.0\nUSER root\n", "root 로 끝난다"),
        ("FROM malkuth/agent-base:0.1.0\nUSER 0\n", "uid 0 으로 끝난다"),
    ],
)
def test_layout_violations_are_refused_before_building(text, why):
    with pytest.raises(MalkuthError) as exc_info:
        check_dockerfile(text)

    assert exc_info.value.code == ErrorCode.VAL_002, why


def test_going_root_and_back_is_allowed():
    """설치 동안 root 로 올라가는 것은 흔하다 — 되돌리지 않고 끝나는 것이 문제다."""
    check_dockerfile(
        "FROM malkuth/agent-base:0.1.0\nUSER root\nRUN apt-get update\nUSER 1000:1000\n"
    )


# --- 조립과 빌드 --------------------------------------------------------------------


async def test_a_build_assembles_the_declared_layout(builder, docker):
    record = await builder.build("custom")

    assert record.status == BuildStatus.BUILT
    assert record.image == image_tag("custom", "0.1.0") == "malkuth/agent-custom:0.1.0"
    context = docker.contexts[0]
    assert context["Dockerfile"] == SKELETON_DOCKERFILE
    assert context["src/agent.py"] == "MARK = 'custom'"
    assert yaml.safe_load(context["manifest.yaml"])["metadata"]["name"] == "custom"
    assert "modules/promptsets/solo/0.1.0/promptset.yaml" in context


async def test_the_temp_directory_does_not_survive_the_build(builder, workspace):
    """작업 트리에 재료를 남기지 않는 것이 이 단계의 존재 이유다."""
    await builder.build("custom")

    assert list((workspace / "work").iterdir()) == []


async def test_the_temp_directory_does_not_survive_a_failure(builder, workspace, docker):
    docker._build_error = ImageBuildError("t", "step 2/3 failed", RuntimeError("boom"))  # noqa: SLF001

    record = await builder.build("custom")

    assert record.status == BuildStatus.FAILED
    assert list((workspace / "work").iterdir()) == []


async def test_a_failure_keeps_the_log(builder, docker):
    """실패 원인은 로그에만 있다 — 없으면 운영자가 손으로 다시 굽는다."""
    docker._build_error = ImageBuildError("t", "step 2/3: package not found", RuntimeError("x"))  # noqa: SLF001

    record = await builder.build("custom")

    assert record.status == BuildStatus.FAILED
    assert "package not found" in record.log
    assert record.error
    assert builder.record_of("custom", "0.1.0").status == BuildStatus.FAILED


async def test_a_user_dockerfile_replaces_the_skeleton(builder, docker):
    builder.materials.put(
        Materials(agent="custom", version="0.1.0", files={"Dockerfile": VALID_DOCKERFILE})
    )

    await builder.build("custom")

    assert docker.contexts[0]["Dockerfile"] == VALID_DOCKERFILE


async def test_a_violating_dockerfile_never_reaches_docker(builder, docker):
    builder.materials.put(
        Materials(agent="custom", version="0.1.0", files={"Dockerfile": "FROM alpine\n"})
    )

    with pytest.raises(MalkuthError) as exc_info:
        await builder.build("custom")

    assert exc_info.value.code == ErrorCode.VAL_002
    assert docker.built == []


async def test_an_agent_without_materials_is_not_built(builder):
    """declarative agent 는 base 이미지로 돈다 — 굽게 만들면 모듈 시스템의 이점이 사라진다."""
    builder.materials.put(Materials(agent="custom", version="0.1.0", files={}))

    with pytest.raises(MalkuthError) as exc_info:
        await builder.build("custom")

    assert exc_info.value.code == ErrorCode.VAL_002
    assert builder.needs_build("custom", "0.1.0") is False


async def test_an_unknown_agent_is_not_found(builder):
    with pytest.raises(MalkuthError) as exc_info:
        await builder.build("nobody")

    assert exc_info.value.code == ErrorCode.NF_001


def test_needs_build_follows_the_materials(builder):
    assert builder.needs_build("custom", "0.1.0") is True
    assert builder.needs_build("custom", "9.9.9") is False
