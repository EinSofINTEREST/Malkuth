"""Deploying a graph into real containers through the control plane (#243).

단위 테스트는 Docker 대역 위에서 돈다. 여기서는 **실제 Docker** 로 base 이미지에
선언을 실어 컨테이너를 세우고, health 로 Ready 를 판정하고, control plane 을
재시작해 다시 붙고, 해체한다 — 첫 실 배포에서 "첫 health 실패 직후 Ready" 가
드러났듯, 이 경계는 대역으로는 건너지 못한다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.test_stack import COMPOSE_FILE, compose_up, docker, fetch, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PORT = 18702
CONTROL_TOKEN = "e2e-control-token"  # noqa: S105
METRICS_PORT = 19702
NETWORK = "malkuth-e2e-net"
GRAPH = "research-pipeline"
AGENTS = ("planner", "researcher", "writer")
CONTAINER_NAME = re.compile(rf"malkuth-({'|'.join(AGENTS)})-\d+")
DEADLINE_S = 120.0
HEADERS = {"Authorization": f"Bearer {CONTROL_TOKEN}"}


def until(predicate, *, what: str, timeout_s: float = DEADLINE_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {timeout_s}s")


def memory_tokens() -> dict[str, str]:
    """Memory Service 가 기동하며 발급한 토큰 — compose 볼륨에서 읽는다."""
    raw = docker(
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "memory-service",
        "cat",
        "/tokens/memory.json",
    )
    return json.loads(raw)


def write_config(directory: Path) -> Path:
    (directory / "e2e.yaml").write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    # 대역 provider 와 Memory Service 가 사는 네트워크에 세운다
                    "network": NETWORK,
                    "agent_env": {"ANTHROPIC_BASE_URL": "http://fake-provider:8000"},
                    "health_check": {"interval_s": 3, "timeout_s": 2},
                },
                "orchestrator": {
                    "run_store": str(directory / "runs.db"),
                    "deployment_store": str(directory / "deployments.db"),
                    "control_port": CONTROL_PORT,
                    "control_token": CONTROL_TOKEN,
                },
            }
        ),
        encoding="utf-8",
    )
    return directory


def start_plane(config_dir: Path, tokens_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "malkuth.orchestrator"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            **os.environ,
            "MALKUTH_ENV": "e2e",
            "MALKUTH_CONFIG_DIR": str(config_dir),
            "MALKUTH_REPO_ROOT": str(REPO_ROOT),
            "MALKUTH_METRICS_PORT": str(METRICS_PORT),
            "MALKUTH_MEMORY_URL": "http://memory-service:8090",
            "MALKUTH_MEMORY_TOKENS_PATH": str(tokens_path),
            # secrets 의 값 원천은 control plane 의 환경이다 (02 Secrets Injection)
            "ANTHROPIC_API_KEY": "e2e-fake-key",
            "SEARCH_API_KEY": "e2e-search-key",
        },
    )


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 방어
            process.kill()


def plane_url() -> str:
    return f"http://127.0.0.1:{CONTROL_PORT}"


def plane_healthy() -> bool:
    try:
        return fetch(f"{plane_url()}/v1/health")["status"] == "ok"
    except Exception:  # noqa: BLE001 — 아직 안 떴다
        return False


def api(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """control plane 호출 — 4xx 도 본문과 함께 돌려준다 (거절 사유를 검증한다)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310
        f"{plane_url()}{path}",
        data=data,
        method=method,
        headers={**HEADERS, "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=DEADLINE_S) as response:  # noqa: S310
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)  # 204 는 본문이 없다
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def deployed_containers() -> dict[str, str]:
    """이 그래프의 에이전트 자리(`malkuth-{agent}-{replica}`)에 서 있는 컨테이너 — 이름 → 상태.

    compose 의 `malkuth-e2e-*` 와 다른 테스트가 남긴 `malkuth-echo-*` 는 제외한다.
    """
    out = docker("ps", "-a", "--format", "{{.Names}}\t{{.Status}}", "--filter", "name=^malkuth-")
    return {
        name: status
        for line in out.splitlines()
        for name, status in [line.split("\t", 1)]
        if CONTAINER_NAME.fullmatch(name)
    }


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    compose_up()
    try:
        yield
    finally:
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    config_dir = write_config(tmp_path)
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    process = start_plane(config_dir, tokens_path)
    state = {"process": process, "config_dir": config_dir, "tokens_path": tokens_path}
    try:
        until(plane_healthy, what="control plane health")
        yield state
    finally:
        stop(state["process"])
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)


def test_deploy_reattach_and_teardown(plane):
    # --- 배포: 선언을 실은 base 이미지 컨테이너가 health 를 지나 Ready 가 된다
    status, record = api("POST", "/v1/deployments", {"graph": GRAPH})
    assert status == 201, record
    assert record["status"] == "ready"
    assert sorted(a["name"] for a in record["agents"]) == sorted(AGENTS)
    assert all(a["image"] == "malkuth/agent-base:0.1.0" for a in record["agents"])

    containers = deployed_containers()
    assert sorted(containers) == [f"malkuth-{a}-0" for a in sorted(AGENTS)]
    # Ready 는 runtime 의 health 판정을 지난 뒤여야 한다 — 지금 바로 healthy 여야 맞다
    for agent in record["agents"]:
        health = fetch(f"http://127.0.0.1:{agent['control_port']}/v1/health")
        assert health["status"] in ("healthy", "degraded"), (agent["name"], health)

    # --- 배선: compose 가 손으로 적던 A2A env 를 runtime 이 그래프에서 만든다
    env = dict(
        line.split("=", 1)
        for line in docker(
            "inspect",
            "malkuth-researcher-0",
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ).splitlines()
        if "=" in line
    )
    assert env["MALKUTH_A2A_EDGES"] == "researcher>planner"
    planner_port = next(a["a2a_port"] for a in record["agents"] if a["name"] == "planner")
    assert env["MALKUTH_A2A_PEERS"] == f"planner=malkuth-planner-0:{planner_port}"
    assert env["ANTHROPIC_BASE_URL"] == "http://fake-provider:8000"
    assert "SEARCH_API_KEY" in env and "ANTHROPIC_API_KEY" in env
    mounts = docker(
        "inspect",
        "malkuth-researcher-0",
        "--format",
        "{{range .Mounts}}{{.Destination}}:{{.RW}} {{end}}",
    )
    assert "/app/manifest.yaml:false" in mounts and "/app/modules/promptsets:false" in mounts

    # --- UI (#245): 같은 프로세스가 화면을 서빙하고, 화면이 만드는 모양의 문서로 저작이 된다
    page = urllib.request.urlopen(f"{plane_url()}/ui/", timeout=10).read().decode()  # noqa: S310
    assert "<title>Malkuth</title>" in page
    draft = {
        "apiVersion": "malkuth/v1",
        "kind": "Graph",
        "metadata": {"name": "ui-e2e", "version": "0.1.0", "description": "made in the ui"},
        "spec": {
            "mode": "mission",
            "goal": "ui e2e",
            "state": {"schema": "malkuth.graphs.schemas:ResearchState"},
            "nodes": [{"id": "planner", "agent": "agents/planner@0.4.0"}],
            "edges": [{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
            "connections": [],
        },
    }
    status, verdict = api("POST", "/v1/validate", {"graphs": [draft]})
    assert status == 200 and verdict["ok"], verdict
    try:
        status, saved = api("PUT", "/v1/graphs/ui-e2e", draft)
        assert status == 200, saved
        status, listed = api("GET", "/v1/graphs")
        assert "ui-e2e" in [g["name"] for g in listed["items"]]
    finally:
        status, _ = api("DELETE", "/v1/graphs/ui-e2e")  # 실패해도 저장소에 남기지 않는다
    assert status == 204

    # --- run 제출 (#244): 주소를 모르고도 배포에 run 을 내고, GET 으로 완주를 본다
    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": record["deployment_id"], "input": {"query": "e2e"}}
    )
    assert status == 202, submitted
    assert submitted["status"] == "running"

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    done = until(finished, what="run to finish")
    assert done["status"] == "completed", done
    assert done["state"].get("report"), done
    status, refused = api("POST", "/v1/runs", {"deployment_id": "dep-nope", "input": {}})
    assert status == 404, refused

    # --- 수정 보호: 배포 중인 에이전트는 덮어쓰지 못한다 (#242 in_use ↔ #243)
    manifest = yaml.safe_load((REPO_ROOT / "agents" / "planner" / "manifest.yaml").read_text())
    manifest["metadata"]["version"] = "99.0.0"
    status, body = api("PUT", "/v1/agents/planner", manifest)
    # #242 의 authoring 은 사용 중 선언을 검증 실패(VAL_002)로 거절한다
    assert status == 400 and "deployed" in body["error"]["message"], body

    # --- 재부착: control plane 을 죽였다 살려도 컨테이너와 기록이 이어진다
    stop(plane["process"])
    plane["process"] = start_plane(plane["config_dir"], plane["tokens_path"])
    until(plane_healthy, what="restarted control plane health")
    status, again = api("GET", f"/v1/deployments/{record['deployment_id']}")
    assert status == 200 and again["status"] == "ready", again
    assert [a["container_id"] for a in again["agents"]] == [
        a["container_id"] for a in record["agents"]
    ]

    # --- 감시/재시작: 죽은 컨테이너를 launcher 가 다시 세우고 기록이 그것을 따라간다
    #     (02 Lifecycle 6 — #213/#215 가 launcher 에 넣은 경로가 이 배포에서 실제로 돈다)
    old_writer = next(a for a in again["agents"] if a["name"] == "writer")
    docker("kill", "malkuth-writer-0")

    def replaced() -> dict | None:
        status, current = api("GET", f"/v1/deployments/{record['deployment_id']}")
        writer = next(a for a in current["agents"] if a["name"] == "writer")
        if writer["container_id"] == old_writer["container_id"]:
            return None
        try:
            health = fetch(f"http://127.0.0.1:{writer['control_port']}/v1/health")
        except Exception:  # noqa: BLE001 — 새 컨테이너가 아직 뜨는 중
            return None
        return writer if health["status"] in ("healthy", "degraded") else None

    new_writer = until(replaced, what="writer restarted after being killed")
    assert new_writer["container_id"] != old_writer["container_id"]
    assert deployed_containers()["malkuth-writer-0"].startswith("Up")

    # --- 해체: 컨테이너가 남지 않고 기록은 남는다
    status, stopped = api("DELETE", f"/v1/deployments/{record['deployment_id']}")
    assert status == 200 and stopped["status"] == "stopped"
    until(lambda: not deployed_containers(), what="containers removed", timeout_s=60)
    status, listed = api("GET", "/v1/deployments")
    assert [d["status"] for d in listed["items"]] == ["stopped"]
