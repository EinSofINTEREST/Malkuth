"""Lifecycle supervision in the launcher.

02 Lifecycle 의 상태 기계는 구현되어 있는데 **프로덕션에서 한 번도 돌지
않았다** — `AgentLifecycle` 을 만드는 곳이 테스트뿐이었다 (#213).

그래서 health 주기 확인도, `malkuth_agent_health` 도, `ReplicaRouter` 의
`accepts_tasks` 도 전부 죽어 있었다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from prometheus_client import CollectorRegistry

from malkuth.core.agent import ComponentHealth, HealthState, HealthStatus
from malkuth.core.manifest import AgentManifest
from malkuth.observability.metrics import Metrics
from malkuth.runtime.docker.engine import DockerEngine
from malkuth.runtime.launcher import AgentLauncher
from malkuth.runtime.lifecycle import AgentState
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import spin, until

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )


class StepSleep:
    """정해진 횟수만 즉시 통과시키고, 그 뒤로는 영원히 대기하는 sleep 대역.

    0초로 통과시키기만 하면 감시 루프가 **busy-loop** 이 되어 회차를 셀 수
    없다 — 실제로 자지 않으면서 결정적이려면 통과 횟수를 쥐고 있어야 한다.
    """

    def __init__(self, passes: int) -> None:
        self.left = passes
        self.waits: list[float] = []
        self._blocked = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        if self.left <= 0:
            await self._blocked.wait()  # 테스트가 끝날 때까지 멈춘다
            return
        self.left -= 1


class ScriptedHealth:
    """정해진 응답을 순서대로 돌려주는 Control API 대역."""

    def __init__(self, results: list[HealthStatus]) -> None:
        self._results = list(results)
        self.calls = 0

    async def health(self) -> HealthStatus:
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return HealthStatus(status=HealthState.HEALTHY)


def healthy() -> HealthStatus:
    return HealthStatus(status=HealthState.HEALTHY)


def sick() -> HealthStatus:
    return HealthStatus(status=HealthState.UNHEALTHY)


def degraded() -> HealthStatus:
    return HealthStatus.aggregate({"mcp:fs": ComponentHealth(state=HealthState.DEGRADED)})


def launcher(sleep: StepSleep, **kwargs) -> AgentLauncher:
    kwargs.setdefault("health_interval_s", 10.0)
    return AgentLauncher(
        engine=DockerEngine(client=FakeDockerClient()), health_sleep=sleep, **kwargs
    )


async def start_with(agents: AgentLauncher, results: list[HealthStatus]):
    """기동하고 **첫 확인 전에** probe 를 갈아끼운다.

    `start()` 는 감시 태스크를 만든 뒤 await 하지 않으므로, 돌아온 직후가
    유일하게 안전한 지점이다. 기본 클라이언트를 두면 실제 HTTP 를 시도한다.
    """
    launched = await agents.start(manifest())
    probe = ScriptedHealth(results)
    launched.client.health = probe.health  # type: ignore[method-assign]
    return launched, probe


# --- 상태 전이 ---------------------------------------------------------------


async def test_a_launched_agent_starts_out_starting():
    """02 Rule 2 — 기동만으로 Ready 가 아니다. health OK 가 있어야 한다."""
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))

    launched = await agents.start(manifest())

    assert launched.lifecycle.state is AgentState.STARTING
    assert not launched.lifecycle.accepts_tasks


async def test_the_first_healthy_check_promotes_to_ready():
    """#213 — 이 배선이 없어 멀쩡히 뜬 에이전트도 태스크를 받지 못했다."""
    agents = launcher(StepSleep(0))
    launched, _ = await start_with(agents, [healthy()])

    await until(lambda: launched.lifecycle.state is AgentState.READY)

    assert launched.lifecycle.state is AgentState.READY
    assert launched.lifecycle.accepts_tasks
    await agents.stop_all()


async def test_repeated_failures_mark_the_agent_unhealthy():
    """02 Rule 3 — 3회 연속 실패가 Unhealthy 다."""
    # 통과 3회 = 확인 4회 (첫 확인은 대기 전이다). 더 돌면 대역이 다시
    # healthy 를 돌려줘 상태가 되돌아간다
    agents = launcher(StepSleep(3))
    launched, _ = await start_with(agents, [healthy(), sick(), sick(), sick()])

    await until(lambda: launched.lifecycle.state is AgentState.UNHEALTHY)

    assert launched.lifecycle.state is AgentState.UNHEALTHY
    assert not launched.lifecycle.accepts_tasks
    await agents.stop_all()


async def test_a_degraded_agent_still_accepts_tasks():
    """degraded 는 살아있는 상태다 — optional 자원이 빠졌을 뿐이다."""
    agents = launcher(StepSleep(0))
    launched, _ = await start_with(agents, [degraded()])

    await until(lambda: launched.lifecycle.state is AgentState.READY)

    assert launched.lifecycle.state is AgentState.READY
    await agents.stop_all()


# --- 메트릭 -----------------------------------------------------------------


async def test_the_health_gauge_is_filled():
    """05 의 `AgentDown` 알림은 이 게이지를 본다 — 채우지 않으면 침묵한다."""
    metrics = Metrics(registry=CollectorRegistry())
    agents = launcher(StepSleep(0), metrics=metrics)
    await start_with(agents, [healthy()])
    gauge = metrics.gauge("malkuth_agent_health").labels(agent="echo")

    await until(lambda: gauge._value.get() == 1.0)

    assert gauge._value.get() == 1.0
    await agents.stop_all()


# --- 정지 -------------------------------------------------------------------


async def test_stopping_moves_the_lifecycle_to_stopped():
    agents = launcher(StepSleep(0))
    launched, _ = await start_with(agents, [healthy()])
    await until(lambda: launched.lifecycle.state is AgentState.READY)

    await agents.stop("echo")

    assert launched.lifecycle.state is AgentState.STOPPED


async def test_stopping_cancels_the_health_loop():
    """정지한 컨테이너를 계속 두드리면 runtime 이 그 대기에 묶인다."""
    agents = launcher(StepSleep(2))
    _, probe = await start_with(agents, [healthy()] * 50)
    await until(lambda: probe.calls > 0)
    seen = probe.calls

    await agents.stop("echo")
    await spin(50)

    assert probe.calls == seen
    assert not agents._monitors


# --- 감시를 켜지 않은 배선 -----------------------------------------------------


async def test_supervision_is_off_by_default():
    """루프의 소유자가 분명해야 한다 — 켜지 않은 조립은 태스크를 만들지 않는다."""
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))

    await agents.start(manifest())

    assert not agents._monitors


async def test_the_loop_uses_the_injected_wait():
    """06 Async 2 — 주입한 대기가 쓰여야 테스트가 초 단위로 늘어지지 않는다."""
    stepper = StepSleep(0)
    agents = launcher(stepper)
    await start_with(agents, [healthy()])

    await until(lambda: bool(stepper.waits))

    assert stepper.waits and all(delay == 10.0 for delay in stepper.waits)
    await agents.stop_all()
