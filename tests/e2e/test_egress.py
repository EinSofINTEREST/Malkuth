"""Egress through the proxy, decided per destination (#293), with no other way out (#280).

떠 있는 에이전트 컨테이너 **안에서** 프록시를 거쳐 나간다 — 배포가 넣어 준 ``HTTPS_PROXY`` 그대로.

- 모델 API: 에이전트 env 에는 키가 없고, 대역 provider 는 프록시가 붙인 키를 받는다
- CONNECT: 선언한 목적지는 통과, 회수하면 컨테이너를 건드리지 않고 다음 CONNECT 부터 거부
- 레지스트리가 멈추면 캐시에 있던 허용은 유지, 처음 묻는 목적지는 거부
- record 모드는 선언 밖 목적지를 거부 판정으로 기록하되 통과시킨다

에이전트는 외부 경로가 없는 내부 네트워크(``AGENTS``)에만 있다. 프록시·Memory Service·control plane
포워더만 그 네트워크와 스택 네트워크에 함께 붙는다 — 대역 provider 는 스택 네트워크에만 있으므로
에이전트가 닿는 길은 프록시뿐이다.

대역 provider 는 사설 네트워크에 있으므로 프록시에 사설 목적지로 명시한다. 선언은 저장소를 복사한
작업 공간에서 researcher 에게만 ``fake-provider:8000`` 을 더한다 — 참조 에이전트는 바꾸지 않는다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import (
    CONTROL_PORT,
    NETWORK,
    REPO_ROOT,
    api,
    deployed_containers,
    memory_tokens,
    plane_healthy,
    start_plane,
    stop,
    until,
    write_config,
)
from tests.e2e.test_a2a_access import CALL as PEER_CALL
from tests.e2e.test_a2a_access import FORWARD, FORWARDER, run_in
from tests.e2e.test_memory_access import CALL as MEMORY_CALL
from tests.e2e.test_memory_access import MEMORY, MEMORY_ALIAS, memory_ready, start_memory
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

ENFORCER_TOKEN = "e2e-enforcer-token"  # noqa: S105 — test_memory_access 의 Memory Service 와 같은 값
PROXY = "malkuth-e2e-egress"
PROXY_KEY = "e2e-proxy-held-key"
TARGET = "fake-provider:8000"
UNDECLARED = f"{MEMORY_ALIAS}:8090"
RESEARCHER, PLANNER = "malkuth-researcher-0", "malkuth-planner-0"
AGENTS = "malkuth-e2e-agents"
"""에이전트 네트워크 — ``--internal``. 이그레스 프록시를 켠 control plane 은 이 격리를 요구한다."""

# 컨테이너 안에서 HTTPS_PROXY 로 CONNECT 를 열고 터널로 GET 한다 — 상태 코드(또는 거부 코드)만 출력
TUNNEL = """
import base64, http.client, os, re, sys, urllib.parse
proxy = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
user = urllib.parse.unquote(proxy.username) + ":" + urllib.parse.unquote(proxy.password)
auth = "Basic " + base64.b64encode(user.encode()).decode()
host, port = sys.argv[1].rsplit(":", 1)
conn = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=15)
conn.set_tunnel(host, int(port), headers={"Proxy-Authorization": auth})
try:
    conn.request("GET", "/healthz")
    print(conn.getresponse().status)
except OSError as err:
    found = re.search(r"(\\d{3})", str(err))
    print(found.group(1) if found else "ERR")
"""


def build_proxy_image() -> None:
    docker(
        "build", "-t", "malkuth/egress-proxy:0.1.0",
        "-f", str(REPO_ROOT / "deployments" / "docker" / "egress-proxy.Dockerfile"), str(REPO_ROOT),
        timeout=900,
    )  # fmt: skip


def agents_network() -> None:
    """외부 경로 없는 에이전트 네트워크를 새로 만든다 — 남은 것이 격리가 아닐 수 있다."""
    for name in (PROXY, MEMORY, FORWARDER, *deployed_containers()):
        docker("rm", "-f", name, check=False)
    docker("network", "rm", AGENTS, check=False)
    docker("network", "create", "--internal", AGENTS)


def start_forwarder() -> None:
    """에이전트 네트워크 안의 ``control-plane`` — 호스트의 control plane 으로 잇는다."""
    docker("rm", "-f", FORWARDER, check=False)
    docker(
        "run", "-d", "--name", FORWARDER,
        "--network", NETWORK, "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--entrypoint", "python", "malkuth/memory-service:0.1.0", "-c", FORWARD,
    )  # fmt: skip
    docker("network", "connect", "--alias", "control-plane", AGENTS, FORWARDER)


def start_proxy(mode: str = "enforce") -> None:
    docker("rm", "-f", PROXY, check=False)
    docker(
        "run", "-d", "--name", PROXY,
        "--network", NETWORK,
        "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "-e", f"MALKUTH_ACCESS_URL=http://host.docker.internal:{CONTROL_PORT}",
        "-e", f"MALKUTH_ACCESS_ENFORCER_TOKEN={ENFORCER_TOKEN}",
        "-e", f"MALKUTH_EGRESS_MODE={mode}",
        "-e", f"MALKUTH_EGRESS_PRIVATE_DESTINATIONS={TARGET},{UNDECLARED}",
        "-e", "MALKUTH_EGRESS_ANTHROPIC_UPSTREAM=http://fake-provider:8000",
        "-e", "MALKUTH_EGRESS_ALLOW_PLAINTEXT_UPSTREAM=true",  # 대역 provider 는 평문이다
        "-e", f"ANTHROPIC_API_KEY={PROXY_KEY}",
        "malkuth/egress-proxy:0.1.0",
    )  # fmt: skip
    docker("network", "connect", "--alias", "malkuth-egress", AGENTS, PROXY)


def proxy_ready() -> bool:
    return docker("inspect", "-f", "{{.State.Health.Status}}", PROXY, check=False) == "healthy"


def workspace(tmp_path: Path) -> Path:
    """저장소 선언을 복사하고 researcher 에게만 대역 provider 목적지를 선언한다."""
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest_path = root / "agents" / "researcher" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["runtime"]["egress"] = [TARGET]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    build_proxy_image()
    config_dir = write_config(
        tmp_path,
        orchestrator={
            "control_host": "0.0.0.0",  # noqa: S104 — 프록시·Memory Service 컨테이너가 닿아야 한다
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": ENFORCER_TOKEN,
            "access_agent_url": "http://control-plane:8700",
        },
    )
    config = yaml.safe_load((config_dir / "e2e.yaml").read_text(encoding="utf-8"))
    config["runtime"]["network"] = AGENTS
    config["runtime"]["egress_proxy"] = {
        "connect_url": "http://malkuth-egress:8080",
        "providers_url": "http://malkuth-egress:8081",
    }
    (config_dir / "e2e.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    env = {
        "MALKUTH_MEMORY_URL": f"http://{MEMORY_ALIAS}:8090",
        "MALKUTH_REPO_ROOT": str(workspace(tmp_path)),
    }
    state = {"start": lambda: start_plane(config_dir, tokens_path, env=env)}
    agents_network()
    state["process"] = state["start"]()
    start_memory(also=(AGENTS,))
    start_forwarder()
    start_proxy()
    try:
        until(plane_healthy, what="control plane health")
        until(memory_ready, what="registry-mode memory service health")
        until(proxy_ready, what="egress proxy health")
        yield state
    finally:
        stop(state["process"])
        for name in (PROXY, MEMORY, FORWARDER, *deployed_containers()):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", AGENTS, check=False)


def tunnel(container: str, target: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["docker", "exec", container, "python", "-c", TUNNEL, target],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    out = result.stdout.strip()
    return out.splitlines()[-1] if out else "CRASH:" + result.stderr[-400:]


def container_env(container: str) -> dict[str, str]:
    lines = docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container)
    return dict(line.split("=", 1) for line in lines.splitlines() if "=" in line)


def provider_keys() -> list[str]:
    script = "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://fake-provider:8000/keys').read().decode())"
    return json.loads(docker("exec", MEMORY, "python", "-c", script))


def started_at(container: str) -> str:
    return docker("inspect", "-f", "{{.State.StartedAt}}", container)


def test_egress_is_decided_per_destination_through_the_proxy(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    started = {name: started_at(name) for name in (RESEARCHER, PLANNER)}

    # --- 모델 API: 키는 프록시에만, 호출은 성공
    env = container_env(RESEARCHER)
    assert PROXY_KEY not in env.values() and "e2e-fake-key" not in env.values(), (
        "모델 키가 들어갔다"
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://malkuth-egress:8081/anthropic"
    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": record["deployment_id"], "input": {"query": "e2e"}}
    )
    assert status == 202, submitted

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    done = until(finished, what="run through the proxy to finish")
    assert done["status"] == "completed", done
    keys = provider_keys()
    assert PROXY_KEY in keys, "대역 provider 가 프록시가 붙인 키를 받지 못했다"
    assert env["MALKUTH_ACCESS_CREDENTIAL"] not in keys, "에이전트 신원이 provider 로 새어 나갔다"

    # --- CONNECT: 선언한 목적지는 통과, 선언하지 않은 에이전트는 거부
    assert tunnel(RESEARCHER, TARGET) == "200"
    assert tunnel(PLANNER, TARGET) == "403"

    # --- 회수: 떠 있는 에이전트의 다음 CONNECT 부터 거부, 되돌리면 재배포 없이 통과
    status, revoked = api(
        "POST",
        "/v1/access/revocations",
        {"agent": "researcher", "kind": "egress", "target": TARGET, "reason": "e2e"},
    )
    assert status == 201, revoked
    until(lambda: tunnel(RESEARCHER, TARGET) == "403", what="CONNECT refused after revocation")
    api("DELETE", f"/v1/access/rules/{revoked['rule_id']}")
    until(lambda: tunnel(RESEARCHER, TARGET) == "200", what="CONNECT allowed after lifting")

    # --- record 모드: 선언 밖 목적지를 기록하되 통과시킨다
    start_proxy(mode="record")
    until(proxy_ready, what="egress proxy health in record mode")
    until(
        lambda: tunnel(PLANNER, TARGET) == "200",
        what="record mode lets an undeclared CONNECT through",
    )
    logs = subprocess.run(  # noqa: S603
        ["docker", "logs", PROXY],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert "egress denied but recorded only" in logs.stdout + logs.stderr
    start_proxy(mode="enforce")
    until(proxy_ready, what="egress proxy health back in enforce mode")
    until(lambda: tunnel(RESEARCHER, TARGET) == "200", what="declared CONNECT after proxy restart")

    # --- 레지스트리 중단: 캐시에 있던 허용은 유지, 처음 묻는 목적지는 거부
    stop(plane["process"])
    until(lambda: not plane_healthy(), what="control plane stopped")
    assert tunnel(RESEARCHER, TARGET) == "200", "레지스트리 중단이 캐시된 허용을 끊었다"
    assert tunnel(RESEARCHER, UNDECLARED) == "503", "레지스트리 없이 처음 묻는 목적지가 허용됐다"

    assert {name: started_at(name) for name in started} == started, (
        "권한 변경이 컨테이너를 재시작했다"
    )


# 컨테이너 안에서 프록시를 거치지 않고 나가 본다 — 이름 해석, 스택 네트워크의 사설 IP, 공인 IP
DIRECT = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    socket.create_connection((host, port), timeout=5).close()
    print("OPEN")
except OSError as err:
    print(type(err).__name__)
"""


def direct(container: str, host: str, port: int) -> str:
    return run_in(container, DIRECT, host, str(port))


def address_on(container: str, network: str) -> str:
    return docker(
        "inspect", "-f", f"{{{{(index .NetworkSettings.Networks \"{network}\").IPAddress}}}}",
        container,
    )  # fmt: skip


def run_completes(deployment_id: str) -> dict:
    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": deployment_id, "input": {"query": "isolated"}}
    )
    assert status == 202, submitted

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    return until(finished, what="run on the isolated network to finish")


def test_isolated_agents_have_no_way_out_but_the_proxy(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    assert record["status"] == "ready", record
    assert docker("port", RESEARCHER, check=False) == "", "내부 네트워크 에이전트가 포트를 게시했다"
    assert docker("network", "inspect", "-f", "{{.Internal}}", AGENTS) == "true"

    # --- 프록시를 거치지 않은 외부 호출은 실패한다: 이름 해석, 직접 IP 둘 다
    provider_ip = address_on("malkuth-e2e-fake-provider-1", NETWORK)
    assert direct(RESEARCHER, "fake-provider", 8000) == "gaierror", "스택 네트워크의 이름이 풀렸다"
    assert direct(RESEARCHER, provider_ip, 8000) != "OPEN", "외부 망의 IP 로 직접 나갔다"
    assert direct(RESEARCHER, "1.1.1.1", 443) != "OPEN", "공인 IP 로 직접 나갔다"
    # --- 프록시 경유만 성공한다 — 같은 목적지를 선언대로
    assert tunnel(RESEARCHER, TARGET) == "200"

    # --- control plane 은 격리된 에이전트를 부른다 (health 는 배포가 ready 로 이미 증명)
    assert run_completes(record["deployment_id"])["status"] == "completed"

    # --- Memory Service 와 A2A peer 호출은 격리 뒤에도 동작한다
    longterm = "local:researcher:longterm"
    assert run_in(RESEARCHER, MEMORY_CALL, "append", "longterm", longterm) == "200"
    until(lambda: run_in(RESEARCHER, PEER_CALL, "iso") == "OK", what="A2A peer call", timeout_s=60)

    # --- 재부착: control plane 을 다시 띄워도 격리된 에이전트에 다시 붙어 부른다
    started = {name: started_at(name) for name in (RESEARCHER, PLANNER)}
    stop(plane["process"])
    plane["process"] = plane["start"]()
    until(plane_healthy, what="control plane back")

    def reattached() -> bool:
        _, current = api("GET", f"/v1/deployments/{record['deployment_id']}")
        return current["status"] == "ready"

    until(reattached, what="isolated deployment reattached")
    assert run_completes(record["deployment_id"])["status"] == "completed"
    assert {name: started_at(name) for name in started} == started, "재부착이 컨테이너를 갈았다"

    # --- 해체: 컨테이너가 사라진다
    status, _ = api("DELETE", f"/v1/deployments/{record['deployment_id']}")
    assert status in (200, 202, 204), status
    until(lambda: not deployed_containers(), what="isolated containers torn down")
