"""The ``malkuth`` command-line interface.

운영자가 프레임워크를 다루는 표면. 명령은 얇게 유지하고 판단은 각 레이어에
위임한다 — CLI 가 로직을 들고 있으면 API/대시보드에서 같은 것을 다시 만들어야
한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from malkuth.cli.integrity import (
    dangling_module_refs,
    ghost_containers,
    orphan_checkpoints,
)
from malkuth.config import load_config
from malkuth.core.errors import MalkuthError
from malkuth.core.manifest import AgentManifest, GroupManifest
from malkuth.deploy import validate_deployment
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
    groups = discover_groups(root / "groups")

    report = validate_deployment(
        [topology],
        manifests=manifests,
        groups=groups,
        resolvable_refs=discover_refs(root / "modules"),
        global_secrets=frozenset(groups["global"].spec.secrets) if "global" in groups else (),
        a2a_port_range=args.a2a_port_range,
    )

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
    groups = discover_groups(root / "groups")
    topologies = [
        GraphTopology.model_validate(load_yaml(path))
        for path in sorted((root / "graphs").glob("*.yaml"))
    ]

    report = validate_deployment(
        topologies,
        manifests=manifests,
        groups=groups,
        resolvable_refs=discover_refs(root / "modules"),
        global_secrets=frozenset(groups["global"].spec.secrets) if "global" in groups else (),
        a2a_port_range=args.a2a_port_range,
    )

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


def port_range(raw: str) -> tuple[int, int]:
    """Parse a ``low-high`` port range."""
    low, _, high = raw.partition("-")
    return int(low), int(high)


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
