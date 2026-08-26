"""Unit tests for the Docker SDK call layer.

daemon 없이 검증한다 — 이 계층의 계약은 에러 변환과 정리 보장이지
Docker 그 자체가 아니다.
"""

from __future__ import annotations

import pytest

from malkuth.core.errors import ErrorCategory, MalkuthError
from malkuth.runtime.docker.engine import (
    ContainerHandle,
    DockerClient,
    DockerEngine,
    agent_of,
    control_port_of,
)
from malkuth.runtime.spec import build_container_spec
from tests.fixtures.builders import make_manifest
from tests.fixtures.fake_docker import FakeDockerClient


def spec(**overrides):
    """manifest 에서 파생한 컨테이너 스펙."""
    return build_container_spec(make_manifest(**overrides))


def engine(client: FakeDockerClient | None = None) -> DockerEngine:
    return DockerEngine(client=client or FakeDockerClient())


# --- 스펙 해석 ----------------------------------------------------------------


def test_agent_name_comes_from_the_label():
    """라벨이 곧 소유자 표시다 — 별도 필드를 만들지 않는다."""
    assert agent_of(spec()) == "test-agent"


def test_control_port_is_found_by_name():
    assert control_port_of(spec()) == 8080


def test_spec_without_a_control_port_is_rejected():
    """control 포트가 없으면 runtime 이 에이전트에 닿을 수 없다."""
    without = spec()
    stripped = type(without)(**{**without.__dict__, "ports": ()})

    with pytest.raises(MalkuthError) as exc_info:
        control_port_of(stripped)

    assert exc_info.value.code == "RT_001"


def test_fake_client_satisfies_the_contract():
    assert isinstance(FakeDockerClient(), DockerClient)


# --- 기동 --------------------------------------------------------------------


async def test_start_creates_and_starts_the_container():
    client = FakeDockerClient()

    handle = await engine(client).start(spec())

    assert handle.agent == "test-agent"
    assert client.started == [handle.container_id]
    assert handle.control_port == 49152


async def test_start_uses_the_declared_network():
    """에이전트는 전용 bridge 에만 붙는다."""
    client = FakeDockerClient()

    await engine(client).start(spec())

    assert client.networks == ["malkuth-net"]
    assert client.created[0]["network"] == "malkuth-net"


async def test_missing_image_is_rt_004():
    """이미지 문제는 설정 문제 — 재시도해도 같다."""
    client = FakeDockerClient(image_error=RuntimeError("not found"))

    with pytest.raises(MalkuthError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_004"
    assert exc_info.value.category is ErrorCategory.RUNTIME
    assert exc_info.value.retryable is False
    assert exc_info.value.details["image"]


async def test_create_failure_is_retryable_rt_001():
    """일시적 자원 부족일 수 있으므로 재시도 가능하다."""
    client = FakeDockerClient(create_error=RuntimeError("no space"))

    with pytest.raises(MalkuthError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_001"
    assert exc_info.value.retryable is True


async def test_network_failure_is_rt_001():
    client = FakeDockerClient(network_error=RuntimeError("network down"))

    with pytest.raises(MalkuthError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_001"


async def test_start_failure_discards_the_half_built_container():
    """반쯤 만들어진 컨테이너를 남기면 유령 컨테이너가 쌓인다."""
    client = FakeDockerClient(start_error=RuntimeError("boom"))

    with pytest.raises(MalkuthError):
        await engine(client).start(spec())

    assert len(client.removed) == 1


async def test_failed_start_is_not_tracked():
    client = FakeDockerClient(start_error=RuntimeError("boom"))
    docker = engine(client)

    with pytest.raises(MalkuthError):
        await docker.start(spec())

    assert docker.started == {}


# --- OOM 감지 -----------------------------------------------------------------


async def test_oom_kill_is_rt_003():
    """OOM 은 메모리 상한을 올리지 않으면 재시도해도 같은 결과다."""
    client = FakeDockerClient(state={"OOMKilled": True, "ExitCode": 137})
    docker = engine(client)
    handle = await docker.start(spec())

    with pytest.raises(MalkuthError) as exc_info:
        await docker.check_exit(handle)

    assert exc_info.value.code == "RT_003"
    assert exc_info.value.retryable is False


async def test_oom_exit_code_alone_is_detected():
    """OOMKilled 플래그가 없어도 137 은 OOM 으로 본다."""
    client = FakeDockerClient(state={"OOMKilled": False, "ExitCode": 137})
    docker = engine(client)
    handle = await docker.start(spec())

    with pytest.raises(MalkuthError) as exc_info:
        await docker.check_exit(handle)

    assert exc_info.value.code == "RT_003"


async def test_clean_exit_is_not_an_error():
    client = FakeDockerClient(state={"OOMKilled": False, "ExitCode": 0})
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.check_exit(handle)


# --- drain -------------------------------------------------------------------


async def test_drain_returns_when_work_finishes():
    docker = engine()
    handle = await docker.start(spec())
    remaining = [2, 1, 0]
    drained = []

    async def sleep(_delay: float) -> None:
        remaining.pop(0)

    await docker.drain(
        handle,
        request_drain=lambda: drained.append(True),
        inflight=lambda: remaining[0],
        sleep=sleep,
        clock=lambda: 0.0,
    )

    assert drained == [True]


async def test_drain_awaits_an_async_request():
    docker = engine()
    handle = await docker.start(spec())
    called = []

    async def request() -> None:
        called.append(True)

    await docker.drain(handle, request_drain=request, inflight=lambda: 0, clock=lambda: 0.0)

    assert called == [True]


async def test_drain_timeout_is_rt_005():
    """남은 태스크를 조용히 버리지 않는다."""
    docker = engine()
    handle = await docker.start(spec())
    ticks = iter([0.0, 10.0, 40.0, 80.0])

    async def sleep(_delay: float) -> None:
        pass

    with pytest.raises(MalkuthError) as exc_info:
        await docker.drain(
            handle,
            request_drain=lambda: None,
            inflight=lambda: 1,
            timeout_s=30.0,
            sleep=sleep,
            clock=lambda: next(ticks),
        )

    assert exc_info.value.code == "RT_005"
    assert exc_info.value.details["inflight"] == 1


# --- 정지 --------------------------------------------------------------------


async def test_stop_sends_the_grace_period():
    client = FakeDockerClient()
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert client.stopped == [(handle.container_id, 30.0)]
    assert client.removed == [handle.container_id]


async def test_stop_untracks_the_agent():
    docker = engine()
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert docker.started == {}


async def test_stop_survives_a_failing_stop():
    """정지 실패가 정리를 막으면 유령 컨테이너가 남는다."""
    client = FakeDockerClient(stop_error=RuntimeError("already gone"))
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert client.removed == [handle.container_id]


async def test_stop_survives_a_failing_removal():
    client = FakeDockerClient(remove_error=RuntimeError("in use"))
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert docker.started == {}


async def test_stop_can_keep_the_container():
    client = FakeDockerClient()
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle, remove=False)

    assert client.removed == []


def test_handle_short_id_is_truncated():
    handle = ContainerHandle(
        agent="a", container_id="0123456789abcdef0123", image="i", control_port=1
    )

    assert handle.short_id == "0123456789ab"
