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

import yaml

from malkuth.cli.control import DEFAULT_CONTROL_URL
from malkuth.cli.integrity import (
    dangling_module_refs,
    ghost_containers,
    orphan_checkpoints,
)
from malkuth.config import load_config
from malkuth.core.errors import MalkuthError
from malkuth.core.manifest import AgentManifest, GroupManifest
from malkuth.deploy import ValidationReport, validate_deployment
from malkuth.observability.logging import configure
from malkuth.orchestrator.topology import GraphTopology

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_OK = 0
EXIT_FAILED = 1
"""검증/점검 실패 — 운영 스크립트가 분기할 수 있도록 0 과 구분한다."""

EXIT_USAGE = 2


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML document.

    YAML 문서를 읽습니다.

    Raises:
        MalkuthError: CONFIG/``CFG_001`` if the file cannot be read or parsed.
    """
    from malkuth.config import config_error

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise config_error("cannot read yaml document", path=str(path)) from err

    if not isinstance(document, dict):
        raise config_error("yaml document must be a mapping", path=str(path))
    return document


def discover_agents(root: Path) -> dict[str, AgentManifest]:
    """Load every agent manifest under a directory.

    디렉토리 아래의 모든 에이전트 manifest 를 읽습니다.
    """
    manifests: dict[str, AgentManifest] = {}
    for path in sorted(root.glob("*/manifest.yaml")):
        manifest = AgentManifest.model_validate(load_yaml(path))
        manifests[manifest.name] = manifest
    return manifests


def discover_groups(root: Path) -> dict[str, GroupManifest]:
    """Load every group definition under a directory."""
    groups: dict[str, GroupManifest] = {}
    for path in sorted(root.glob("*.yaml")):
        group = GroupManifest.model_validate(load_yaml(path))
        groups[group.metadata.name] = group
    return groups


def discover_refs(root: Path) -> frozenset[str]:
    """Collect every published module ref under the registry roots.

    registry 루트 아래의 게시된 모듈 ref 를 모읍니다 — 디렉토리 구조가
    ``{type}/{name}/{version}/`` 이므로 경로에서 ref 를 복원합니다.
    """
    refs: set[str] = set()
    for module_type in ("skillsets", "promptsets", "memorysets"):
        type_root = root / module_type
        if not type_root.is_dir():
            continue
        for version_dir in sorted(type_root.glob("*/*")):
            if version_dir.is_dir():
                refs.add(f"{module_type}/{version_dir.parent.name}@{version_dir.name}")
    return frozenset(refs)


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
    groups = discover_groups(root / "groups")
    return validate_deployment(
        topologies,
        manifests=discover_agents(root / "agents"),
        groups=groups,
        resolvable_refs=discover_refs(root / "modules"),
        global_secrets=frozenset(groups["global"].spec.secrets) if "global" in groups else (),
        a2a_port_range=a2a_port_range,
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
    manifests = discover_agents(root / "agents")

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
    manifests = discover_agents(root / "agents")
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
    root = Path(args.root)
    manifests = discover_agents(root / "agents")
    groups = discover_groups(root / "groups")
    graphs = sorted(p.stem for p in (root / "graphs").glob("*.yaml"))

    emit(
        {
            "agents": sorted(manifests),
            "groups": sorted(groups),
            "graphs": graphs,
            "modules": sorted(discover_refs(root / "modules")),
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """Show the resolved configuration for an environment.

    환경의 해석된 설정을 보여줍니다 — 환경변수 오버라이드가 반영된 결과입니다.
    """
    config = load_config(args.environment, config_dir=args.config_dir)
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


def cmd_run(args: argparse.Namespace) -> int:
    """Submit a mission run and wait for it to finish.

    mission run 을 제출하고 완주를 기다립니다.

    실행에는 살아있는 에이전트가 필요합니다 — 컨테이너가 없으면 노드 실행이
    ``GRAPH_002`` 로 실패하므로, 여기서는 **제출 전 계약 검증**까지 수행하고
    실행 경로를 명시적으로 보고합니다.
    """
    import asyncio

    from malkuth.orchestrator.checkpoint import build_checkpointer, close_checkpointer
    from malkuth.orchestrator.submit import RunSubmitter
    from malkuth.runtime.nodes import ControlNodeRuntime

    root = Path(args.root)
    topology = GraphTopology.model_validate(load_yaml(Path(args.graph)))
    payload = json.loads(args.input) if args.input else {}

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
    checkpointer = build_checkpointer(args.checkpointer)
    submitter = RunSubmitter(
        runtime=ControlNodeRuntime(clients=_control_clients(args)),
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


def _control_clients(args: argparse.Namespace) -> dict[str, Any]:
    """Build Control API clients from ``--agent name=url`` pairs.

    ``--agent`` 로 받은 주소로 Control API 클라이언트를 만듭니다 —
    CLI 가 컨테이너 포트를 추측하지 않고 호출자가 명시합니다.

    Control API 는 per-agent 토큰을 요구하므로 **토큰도 함께 실어야** 합니다 —
    빠뜨리면 모든 노드 호출이 401 이 됩니다. ``--agent-token`` 이 우선하고,
    없으면 ``MALKUTH_AGENT_TOKEN`` 을 봅니다 (compose 와 같은 키).
    """
    from malkuth.runtime.control import ControlClient
    from malkuth.runtime.tokens import AGENT_TOKEN_ENV

    # 빈 문자열은 미설정과 같게 다룬다 — compose 의 ${VAR:-default} 와 같은 규칙
    token = getattr(args, "agent_token", None) or os.environ.get(AGENT_TOKEN_ENV) or None

    clients: dict[str, Any] = {}
    for entry in args.agent or ():
        name, _, url = entry.partition("=")
        if not name or not url:
            continue
        clients[name] = ControlClient(url, agent=name, token=token)
    return clients


def port_range(raw: str) -> tuple[int, int]:
    """Parse a ``low-high`` port range."""
    low, _, high = raw.partition("-")
    return int(low), int(high)


def _control_client(args: argparse.Namespace) -> Any:
    """이 명령이 말할 Control Plane — 주소는 플래그 또는 기본값."""
    from malkuth.cli.control import ControlClient

    return ControlClient(getattr(args, "control_url", None) or DEFAULT_CONTROL_URL)


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
    config.add_argument("environment", nargs="?", default="dev")
    config.add_argument("--config-dir", default="configs", dest="config_dir")
    config.set_defaults(handler=cmd_config)

    run = subcommands.add_parser("run", help="submit a mission run")
    run.add_argument("graph", help="path to the graph topology yaml")
    run.add_argument("--input", default=None, help="initial state as json")
    run.add_argument("--run-id", default=None, dest="run_id")
    run.add_argument("--checkpointer", default="default")
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
