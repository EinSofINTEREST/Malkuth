"""The ``malkuth`` command-line interface.

운영자가 프레임워크를 다루는 표면. 명령은 얇게 유지하고 판단은 각 레이어에
위임한다 — CLI 가 로직을 들고 있으면 API/대시보드에서 같은 것을 다시 만들어야
한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from malkuth.authoring import Author
from malkuth.catalog import Catalog, load_yaml
from malkuth.cli.control import CONTROL_TOKEN_ENV, DEFAULT_CONTROL_URL
from malkuth.cli.integrity import (
    dangling_module_refs,
    ghost_containers,
    orphan_checkpoints,
)
from malkuth.config import DEFAULT_CONFIG_DIR, ENVIRONMENT_ENV, load_config, resolve_environment
from malkuth.core.errors import MalkuthError
from malkuth.deploy import ValidationReport
from malkuth.observability.logging import configure
from malkuth.orchestrator.topology import GraphTopology

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_OK = 0
EXIT_FAILED = 1
"""검증/점검 실패 — 운영 스크립트가 분기할 수 있도록 0 과 구분한다."""

EXIT_USAGE = 2


def validate_root(
    root: Path,
    topologies: Sequence[GraphTopology],
    *,
    a2a_port_range: tuple[int, int] | None = None,
) -> ValidationReport:
    """Validate topologies against the repository's declarations.

    저장소 선언을 기준으로 토폴로지를 검증합니다.

    세 명령(`deploy` / `validate` / `run`)이 **같은 입력으로 같은 판정**을
    내리도록 한 곳에 모읍니다 — 흩어지면 한 명령만 통과하는 상태가 생깁니다.
    """
    # 조립은 Author.validate 한 곳 — CLI 는 지목한 그래프만 판정한다 (with_saved_graphs=False)
    return Author(catalog=Catalog.under(root), a2a_port_range=a2a_port_range).validate(
        graphs=topologies, with_saved_graphs=False
    )


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """Print a command result.

    명령 결과를 출력합니다 — ``--json`` 은 스크립트가 파싱할 수 있는 형태입니다.
    """
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    for key, value in payload.items():
        if isinstance(value, list):
            print(f"{key}:")
            for item in value:
                print(f"  - {item}")
        else:
            print(f"{key}: {value}")


def cmd_deploy(args: argparse.Namespace) -> int:
    """Validate a deployment before anything starts.

    배포 전 계약을 검증합니다. **검증 실패 시 아무것도 기동하지 않습니다** —
    이것이 이 명령의 요점입니다.
    """
    root = Path(args.root)
    topology = GraphTopology.model_validate(load_yaml(Path(args.graph)))
    manifests = Catalog.under(root).agents().items

    report = validate_root(root, [topology], a2a_port_range=args.a2a_port_range)

    emit(
        {
            "graph": topology.metadata.name,
            "validated": len(manifests),
            "status": "ok" if report.ok else "failed",
            "failures": [f"{f.check}: {f.message}" for f in report.findings],
        },
        as_json=args.json,
    )
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate every graph in the repository.

    저장소의 모든 그래프를 검증합니다 — 배포 전 일괄 점검용입니다.
    """
    root = Path(args.root)
    manifests = Catalog.under(root).agents().items
    topologies = [
        GraphTopology.model_validate(load_yaml(path))
        for path in sorted((root / "graphs").glob("*.yaml"))
    ]

    report = validate_root(root, topologies, a2a_port_range=args.a2a_port_range)

    emit(
        {
            "graphs": len(topologies),
            "agents": len(manifests),
            "status": "ok" if report.ok else "failed",
            "failures": [f"{f.check}: {f.message}" for f in report.findings],
        },
        as_json=args.json,
    )
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_status(args: argparse.Namespace) -> int:
    """Summarise what is declared in the repository.

    저장소에 선언된 것을 요약합니다. 실행 중 상태 조회는 runtime 연결이
    필요하므로, 이 명령은 **선언 상태**를 보고합니다.
    """
    catalog = Catalog.under(Path(args.root))
    agents, groups, graphs = catalog.agents(), catalog.groups(), catalog.graphs()

    payload: dict[str, Any] = {
        "agents": sorted(agents.items),
        "groups": sorted(groups.items),
        "graphs": sorted(graphs.items),
        "modules": sorted(catalog.module_refs()),
    }
    # 깨진 선언은 조용히 빼지 않는다 — 운영자가 빠진 줄 모른다
    problems = [*agents.problems, *groups.problems, *graphs.problems]
    if problems:
        payload["problems"] = [f"{p.path}: [{p.code}] {p.message}" for p in problems]
    emit(payload, as_json=args.json)
    return EXIT_OK if not problems else EXIT_FAILED


def cmd_config(args: argparse.Namespace) -> int:
    """Show the resolved configuration for an environment.

    환경의 해석된 설정을 보여줍니다 — 환경변수 오버라이드가 반영된 결과입니다.
    """
    config = load_config(resolve_environment(args.environment), config_dir=args.config_dir)
    emit(config.model_dump(mode="json"), as_json=True)
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """Report integrity discrepancies.

    기록과 실체의 불일치를 보고합니다. 입력은 운영 도구가 수집한 상태를
    받습니다 — CLI 가 저장소나 Docker 에 직접 붙지 않습니다.
    """
    state = load_yaml(Path(args.state))

    found = [
        *orphan_checkpoints(state.get("runs", []), state.get("checkpoints", [])),
        *dangling_module_refs(state.get("deployed_refs", {}), state.get("resolvable_refs", [])),
        *ghost_containers(state.get("running", []), state.get("known_agents", [])),
    ]

    emit(
        {
            "status": "ok" if not found else "discrepancies",
            "discrepancies": [f"{d.kind}: {d.subject} — {d.detail}" for d in found],
        },
        as_json=args.json,
    )
    return EXIT_OK if not found else EXIT_FAILED


def checkpointer_for(args: argparse.Namespace) -> Any:
    """Build the checkpointer a run should use.

    설정이 backend 와 URL 을 쥐고, ``--checkpointer`` 는 override 입니다.
    URL 을 받을 자리가 없어 `--checkpointer postgres` 가 **어떤 조합으로도**
    동작하지 않았습니다 (#220) — `configs/prod.yaml` 의 `checkpointer: postgres`
    선언도 도달 불가능했습니다.

    자격증명은 파일에 굽지 않는 것이 기본이므로 URL 은
    ``MALKUTH_ORCHESTRATOR__CHECKPOINTER_URL`` 로 덮어쓸 수 있습니다.
    """
    from malkuth.orchestrator.checkpoint import build_checkpointer

    orchestrator = orchestrator_config(args)
    return build_checkpointer(
        args.checkpointer or orchestrator.checkpointer,
        url=orchestrator.checkpointer_url,
    )


def orchestrator_config(args: argparse.Namespace) -> Any:
    """Resolve the orchestrator settings this invocation should use."""
    return load_config(
        resolve_environment(getattr(args, "environment", None)),
        config_dir=getattr(args, "config_dir", DEFAULT_CONFIG_DIR),
    ).orchestrator


def run_manager_for(args: argparse.Namespace) -> Any:
    """Build the run manager, recording to the configured store when there is one.

    기록을 남기지 않으면 **프로세스 밖에서 run 을 볼 수 없습니다** — control plane
    이 떠 있어도 빈 목록을 돌려주고, `run-drain` 요청도 구동 프로세스에 전달되지
    않습니다 (#221). 저장소는 설정이 정합니다.
    """
    from malkuth.orchestrator.run import RunManager
    from malkuth.orchestrator.runstore import SqliteRunStore

    orchestrator = orchestrator_config(args)
    store = None if orchestrator.run_store is None else SqliteRunStore(path=orchestrator.run_store)
    return RunManager(
        max_concurrent_runs=orchestrator.max_concurrent_runs,
        max_service_runs=orchestrator.max_service_runs,
        store=store,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Submit a mission run and wait for it to finish.

    mission run 을 제출하고 완주를 기다립니다.

    실행에는 살아있는 에이전트가 필요합니다 — 컨테이너가 없으면 노드 실행이
    ``GRAPH_002`` 로 실패하므로, 여기서는 **제출 전 계약 검증**까지 수행하고
    실행 경로를 명시적으로 보고합니다.
    """
    import asyncio

    from malkuth.orchestrator.checkpoint import close_checkpointer
    from malkuth.orchestrator.submit import RunSubmitter
    from malkuth.runtime.nodes import ControlNodeRuntime

    payload = json.loads(args.input) if args.input else {}
    if getattr(args, "deployment", None):
        return _run_on_deployment(args, payload)
    if args.graph is None:
        emit(
            {"status": "rejected", "error": "graph path or --deployment is required"},
            as_json=args.json,
        )
        return EXIT_FAILED

    root = Path(args.root)
    topology = GraphTopology.model_validate(load_yaml(Path(args.graph)))

    report = validate_root(root, [topology])
    if not report.ok:
        # 검증에 실패한 그래프를 굴리면 노드 실행 중에야 실패한다
        emit(
            {
                "graph": topology.metadata.name,
                "status": "rejected",
                "failures": [f"{f.check}: {f.message}" for f in report.findings],
            },
            as_json=args.json,
        )
        return EXIT_FAILED

    # 에이전트 주소는 runtime 이 제공한다 — CLI 가 포트를 추측하지 않는다
    from malkuth.observability.metrics import Metrics

    # CLI 는 단발 실행이라 scrape 대상이 아니다 — 서버는 띄우지 않고 registry 만
    # 물려 각 계층의 집계가 실제로 돌게 한다 (상주 프로세스는 agentd 쪽)
    checkpointer = checkpointer_for(args)
    submitter = RunSubmitter(
        runtime=ControlNodeRuntime(clients=_control_clients(args)),
        manager=run_manager_for(args),
        checkpointer=checkpointer,
        metrics=Metrics(),
    )

    if getattr(args, "service", False):
        return _run_service(submitter, topology, payload, args, checkpointer=checkpointer)

    async def run_once() -> Any:
        # 여는 쪽이 닫는다 — checkpointer 를 만든 것이 여기이므로 정리도 여기서.
        # 프로세스 종료에 기대면 소유자가 코드에 드러나지 않는다
        try:
            return await submitter.submit(topology, payload, run_id=args.run_id)
        finally:
            await close_checkpointer(checkpointer)

    result = asyncio.run(run_once())

    emit(
        {
            "run_id": result.run_id,
            "graph": result.graph,
            "status": str(result.status),
            "state": result.state,
            "error": result.error.message if result.error else None,
        },
        as_json=args.json,
    )
    return EXIT_OK if result.ok else EXIT_FAILED


def _run_service(
    submitter: Any,
    topology: Any,
    payload: dict[str, Any],
    args: argparse.Namespace,
    *,
    checkpointer: Any = None,
) -> int:
    """Drive a service graph until interrupted or bounded.

    상주 그래프를 구동합니다. ``--iterations`` 가 없으면 인터럽트까지 돌고,
    인터럽트는 **즉시 취소가 아니라 drain** 입니다 — 진행 중 iteration 을 마친
    뒤 정지하므로 반쯤 진행된 회차가 남지 않습니다.
    """
    import asyncio
    import contextlib
    import signal

    async def drive() -> Any:
        handle = await submitter.start_service(
            topology, payload, run_id=args.run_id, max_iterations=args.iterations
        )
        task = submitter.services[handle.run_id]

        # 인터럽트를 **취소가 일어나기 전에** 잡아 drain 을 요청한다.
        # 취소된 뒤에 정리하려 하면 그 대기까지 함께 취소되고, shield 로 감싸도
        # 바깥이 끝나면서 이벤트 루프가 함께 닫혀 완료를 볼 수 없다
        loop = asyncio.get_running_loop()
        installed = False
        with contextlib.suppress(NotImplementedError):  # Windows 는 미지원
            loop.add_signal_handler(signal.SIGINT, handle.request_drain)
            installed = True

        try:
            await task
        finally:
            if installed:
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(signal.SIGINT)
            # 상주 실행이라 더 중요하다 — 정리하지 않으면 커넥션을 물고 있는다
            if checkpointer is not None:
                from malkuth.orchestrator.checkpoint import close_checkpointer

                await close_checkpointer(checkpointer)
        return handle

    try:
        handle = asyncio.run(drive())
    except KeyboardInterrupt:
        emit({"graph": topology.metadata.name, "status": "interrupted"}, as_json=args.json)
        return EXIT_FAILED

    emit(
        {
            "run_id": handle.run_id,
            "graph": topology.metadata.name,
            "status": str(handle.status),
            "iterations": handle.iteration,
            "error": handle.error.message if handle.error else None,
        },
        as_json=args.json,
    )
    return EXIT_OK if handle.error is None else EXIT_FAILED


def _finished_statuses() -> frozenset[str]:
    """서버 계약에서 끌어온다 — 여기 따로 적으면 `RunStatus` 와 어긋난 채 남는다."""
    from malkuth.orchestrator.run import RunStatus

    live = (RunStatus.RUNNING, RunStatus.DRAINING)
    return frozenset(str(s) for s in RunStatus if s not in live)


def _run_on_deployment(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    """배포에 run 을 낸다 — 에이전트 주소를 적지 않는다 (#244).

    control plane 이 즉시 run_id 를 돌려주고, ``--no-wait`` 가 아니면 끝날 때까지
    ``GET /v1/runs/{id}`` 로 본다. service run 은 끝이 없으므로 기다리지 않는다.
    """
    import time

    try:
        client = _control_client(args)
        submitted = client.submit_run(
            args.deployment, payload, mode=getattr(args, "mode", None), run_id=args.run_id
        )
        run_id = submitted["run_id"]
        if getattr(args, "no_wait", False) or submitted.get("mode") == "service":
            emit(submitted, as_json=args.json)
            return EXIT_OK
        deadline = time.monotonic() + args.wait_timeout_s
        finished = _finished_statuses()
        current = submitted
        while current.get("status") not in finished:
            if time.monotonic() >= deadline:
                emit({**current, "error": "timed out waiting for the run"}, as_json=args.json)
                return EXIT_FAILED
            time.sleep(args.poll_s)
            current = client.get_run(run_id)
    except MalkuthError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(current, as_json=args.json)
    return EXIT_OK if current.get("status") == "completed" else EXIT_FAILED


def _control_clients(args: argparse.Namespace) -> dict[str, Any]:
    """Build Control API clients from ``--agent name=url`` pairs.

    ``--agent`` 로 받은 주소로 Control API 클라이언트를 만듭니다 —
    CLI 가 컨테이너 포트를 추측하지 않고 호출자가 명시합니다.

    Control API 는 per-agent 토큰을 요구하므로 **토큰도 함께 실어야** 합니다 —
    빠뜨리면 모든 노드 호출이 401 이 됩니다. ``--agent-token`` 이 우선하고,
    없으면 ``MALKUTH_AGENT_TOKEN`` 을 봅니다 (compose 와 같은 키).
    """
    from malkuth.core.errors import NETWORK_RETRY
    from malkuth.runtime.control import ControlClient
    from malkuth.runtime.tokens import AGENT_TOKEN_ENV

    # 빈 문자열은 미설정과 같게 다룬다 — compose 의 ${VAR:-default} 와 같은 규칙
    token = getattr(args, "agent_token", None) or os.environ.get(AGENT_TOKEN_ENV) or None

    clients: dict[str, Any] = {}
    for entry in args.agent or ():
        name, _, url = entry.partition("=")
        if not name or not url:
            continue
        # 05 Retry Layering — runtime 이 Control API 재시도 주체다.
        # 여기서 켜지 않으면 정책은 정의만 되고 아무 일도 하지 않는다.
        # 읽기(health/card)만 재시도한다 — invoke 는 부수효과를 낳고
        # node 재시도(NodeSpec.retry)와 곱해진다
        clients[name] = ControlClient(url, agent=name, token=token, retry=NETWORK_RETRY)
    return clients


def port_range(raw: str) -> tuple[int, int]:
    """Parse a ``low-high`` port range."""
    low, _, high = raw.partition("-")
    return int(low), int(high)


def _control_client(args: argparse.Namespace) -> Any:
    """이 명령이 말할 Control Plane — 주소는 플래그 또는 기본값."""
    from malkuth.cli.control import ControlClient

    # 빈 문자열은 미설정과 같게 — --agent-token 과 같은 규칙
    token = getattr(args, "control_token", None) or os.environ.get(CONTROL_TOKEN_ENV) or None
    return ControlClient(getattr(args, "control_url", None) or DEFAULT_CONTROL_URL, token=token)


def _report_control_failure(err: MalkuthError, *, as_json: bool) -> int:
    """조작 실패를 사람이 읽을 형태로 — 연결 거부를 그대로 던지지 않는다."""
    emit(
        {"status": "failed", "error_code": str(err.code), "message": err.message, **err.details},
        as_json=as_json,
    )
    return EXIT_FAILED


def cmd_run_list(args: argparse.Namespace) -> int:
    """List runs the control plane knows about.

    Control Plane 이 아는 run 목록을 보여줍니다 — 다른 프로세스가 띄운
    run 도 포함됩니다.
    """
    try:
        listed = _control_client(args).list_runs(mode=args.mode)
    except MalkuthError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(
        {
            "runs": [
                {
                    "run_id": run["run_id"],
                    "graph": run["graph"],
                    "mode": run["mode"],
                    "status": run["status"],
                    "iteration": run["iteration"],
                    "drain_requested": run["drain_requested"],
                }
                for run in listed
            ]
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_run_drain(args: argparse.Namespace) -> int:
    """Ask a run to stop after its current iteration.

    진행 중 iteration 을 마친 뒤 정지하도록 요청합니다 — **요청만 남기고
    돌아옵니다.** 실제 정지는 구동 프로세스가 수행하므로, 이 명령이 성공해도
    run 은 아직 돌고 있을 수 있습니다.
    """
    try:
        result = _control_client(args).drain(args.run_id)
    except MalkuthError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(
        {
            "run_id": result["run_id"],
            "status": result["status"],
            "drain_requested": result["drain_requested"],
            "note": "the run stops after its current iteration",
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_run_resume(args: argparse.Namespace) -> int:
    """Restart a halted run from its last iteration.

    ``GRAPH_005`` 로 정지한 run 을 마지막 iteration **다음**부터 재개합니다
    (05 Incident Response).
    """
    try:
        result = _control_client(args).resume(args.run_id)
    except MalkuthError as err:
        return _report_control_failure(err, as_json=args.json)

    emit({"run_id": result["run_id"], "status": result.get("status", "resumed")}, as_json=args.json)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    CLI 파서를 만듭니다.
    """
    parser = argparse.ArgumentParser(prog="malkuth", description="Malkuth framework CLI")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    subcommands = parser.add_subparsers(dest="command", required=True)

    deploy = subcommands.add_parser("deploy", help="validate a graph before deploying")
    deploy.add_argument("graph", help="path to the graph topology yaml")
    deploy.add_argument("--a2a-port-range", type=port_range, default=None, dest="a2a_port_range")
    deploy.set_defaults(handler=cmd_deploy)

    validate = subcommands.add_parser("validate", help="validate every graph")
    validate.add_argument("--a2a-port-range", type=port_range, default=None, dest="a2a_port_range")
    validate.set_defaults(handler=cmd_validate)

    status = subcommands.add_parser("status", help="summarise declared artifacts")
    status.set_defaults(handler=cmd_status)

    config = subcommands.add_parser("config", help="show resolved configuration")
    config.add_argument("environment", nargs="?", default=None)
    config.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR, dest="config_dir")
    config.set_defaults(handler=cmd_config)

    run = subcommands.add_parser("run", help="submit a mission run")
    run.add_argument("graph", nargs="?", default=None, help="path to the graph topology yaml")
    run.add_argument(
        "--deployment",
        default=None,
        help="submit to a deployment through the control plane instead of driving the run here",
    )
    run.add_argument("--no-wait", action="store_true", dest="no_wait")
    run.add_argument(
        "--control-url",
        default=None,
        dest="control_url",
        help=f"control plane address for --deployment (default: {DEFAULT_CONTROL_URL})",
    )
    run.add_argument(
        "--control-token",
        default=None,
        dest="control_token",
        help=f"control plane token (defaults to ${CONTROL_TOKEN_ENV})",
    )
    run.add_argument("--wait-timeout", type=float, default=600.0, dest="wait_timeout_s")
    run.add_argument("--poll", type=float, default=1.0, dest="poll_s")
    run.add_argument("--input", default=None, help="initial state as json")
    run.add_argument("--run-id", default=None, dest="run_id")
    run.add_argument(
        "--checkpointer",
        default=None,
        help="override the configured checkpoint backend (memory|postgres|redis)",
    )
    run.add_argument(
        "--env",
        default=None,
        dest="environment",
        help=f"configuration environment (default: ${ENVIRONMENT_ENV}, else dev)",
    )
    run.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR, dest="config_dir")
    run.add_argument(
        "--agent",
        action="append",
        metavar="NAME=URL",
        help="agent control api address (repeatable)",
    )
    run.add_argument(
        "--agent-token",
        default=None,
        dest="agent_token",
        help="control api token (defaults to $MALKUTH_AGENT_TOKEN)",
    )
    run.add_argument(
        "--service",
        action="store_true",
        help="drive a service graph's perpetual loop instead of a mission run",
    )
    run.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="stop a service run after N iterations (default: until interrupted)",
    )
    run.set_defaults(handler=cmd_run)

    # 다른 프로세스의 run 을 조작하는 명령 — 별도 subcommand 로 둔다.
    # `run` 아래 subparser 로 넣으면 기존 `malkuth run <graph>` 형태가 깨진다
    for name, handler, helptext in (
        ("run-list", cmd_run_list, "list runs the control plane knows about"),
        ("run-drain", cmd_run_drain, "ask a run to stop after its current iteration"),
        ("run-resume", cmd_run_resume, "resume a halted run from its last iteration"),
    ):
        command = subcommands.add_parser(name, help=helptext)
        if name != "run-list":
            command.add_argument("run_id", help="the run to operate on")
        else:
            command.add_argument(
                "--mode", default=None, choices=["mission", "service"], help="narrow by run mode"
            )
        command.add_argument(
            "--control-url",
            default=None,
            dest="control_url",
            help=f"control plane address (default: {DEFAULT_CONTROL_URL})",
        )
        command.add_argument(
            "--control-token",
            default=None,
            dest="control_token",
            help=f"control plane token (defaults to ${CONTROL_TOKEN_ENV})",
        )
        command.set_defaults(handler=handler)

    check = subcommands.add_parser("check", help="report integrity discrepancies")
    check.add_argument("state", help="path to a yaml document describing observed state")
    check.set_defaults(handler=cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    CLI 를 실행합니다. 구조화 에러는 사람이 읽을 수 있게 출력하고 비정상 종료
    코드를 돌려줍니다 — 스택 트레이스를 그대로 뱉으면 운영자가 원인을 찾기
    어렵습니다.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 로그는 stderr 로 — stdout 은 명령 결과 전용이다. 섞이면 `--json` 출력을
    # 스크립트가 파싱할 수 없다 (출력과 진단의 분리는 CLI 의 기본 계약)
    configure(json_output=bool(args.json), stream_name="stderr")

    try:
        exit_code: int = args.handler(args)
    except MalkuthError as err:
        print(f"error [{err.code}] {err.message}", file=sys.stderr)
        for key, value in err.details.items():
            print(f"  {key}: {value}", file=sys.stderr)
        return EXIT_FAILED
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
