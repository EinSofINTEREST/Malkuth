"""Metric injection at the agentd startup path.

계측 로직이 있어도 **registry 를 만들어 주입하지 않으면** 런타임에서는 아무
것도 흐르지 않는다 (#95). 여기서는 그 배선만 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from malkuth.agentd.__main__ import build_executor, load_manifest
from malkuth.observability.metrics import Metrics

REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = REPO_ROOT / "agents" / "researcher" / "manifest.yaml"


@pytest.fixture(autouse=True)
def _standard_path(monkeypatch):
    """표준 executor 경로 — echo 대역을 쓰지 않는다."""
    monkeypatch.delenv("MALKUTH_EXECUTOR", raising=False)
    monkeypatch.setenv("MALKUTH_ROOT", str(REPO_ROOT))


async def test_executor_receives_the_registry():
    metrics = Metrics(registry=CollectorRegistry())

    executor = await build_executor(load_manifest(RESEARCHER), metrics=metrics)

    assert executor._telemetry is not None


async def test_labels_come_from_the_manifest():
    """빈 문자열로 남으면 그룹별·모델별 집계가 무의미해진다."""
    metrics = Metrics(registry=CollectorRegistry())

    executor = await build_executor(load_manifest(RESEARCHER), metrics=metrics)

    telemetry = executor._telemetry
    assert telemetry._agent == "researcher"
    assert telemetry._group == "research"
    assert telemetry._provider == "anthropic"
    assert telemetry._model


async def test_executor_works_without_metrics():
    executor = await build_executor(load_manifest(RESEARCHER))

    assert executor._telemetry is None


def test_the_exposed_registry_is_the_one_we_fill(monkeypatch):
    """registry 를 넘기지 않으면 prometheus 기본 registry 를 노출하게 된다 —
    우리가 채우는 곳과 달라 endpoint 가 늘 비어 있다."""
    from malkuth.agentd import __main__ as entry

    metrics = Metrics(registry=CollectorRegistry())
    exposed: dict[str, object] = {}

    def capture(port: int, *, registry=None) -> None:
        exposed["port"] = port
        exposed["registry"] = registry

    # _setup_observability 가 함수 안에서 import 하므로 원본 모듈을 패치한다
    monkeypatch.setattr("malkuth.observability.metrics.Metrics", lambda: metrics)
    monkeypatch.setattr("malkuth.observability.metrics.start_metrics_server", capture)
    monkeypatch.setenv("MALKUTH_METRICS_PORT", "9123")

    entry._setup_observability()

    assert exposed["registry"] is metrics.registry
    assert exposed["port"] == 9123


async def test_recorded_metrics_reach_the_exposed_registry():
    """endpoint 를 긁으면 0 이 아닌 값이 나와야 한다 — 배선의 최종 확인이다."""
    registry = CollectorRegistry()
    metrics = Metrics(registry=registry)

    executor = await build_executor(load_manifest(RESEARCHER), metrics=metrics)
    executor._telemetry.task_finished(status="completed", duration_s=0.01)

    scraped = generate_latest(registry).decode("utf-8")
    assert 'malkuth_agent_tasks_total{agent="researcher"' in scraped
    assert 'group="research"' in scraped
