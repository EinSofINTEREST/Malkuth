"""Container specification from an agent manifest.

Manifest 를 Docker 컨테이너 스펙으로 변환한다. 02 의 보안 기본값
(non-root / cap-drop ALL / read-only rootfs / PID limit) 을 코드로 강제해,
선언에서 빠뜨려도 안전하지 않은 컨테이너가 만들어지지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from malkuth.core.manifest import AgentManifest

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_NETWORK: Final = "malkuth-net"
DEFAULT_CONTROL_PORT: Final = 8080
DEFAULT_PID_LIMIT: Final = 256
DEFAULT_BASE_IMAGE: Final = "malkuth/agent-base:0.1.0"

# 컨테이너가 쓸 수 있어야 하는 임시 경로 — read-only rootfs 위의 tmpfs
_WRITABLE_TMPFS: Final = ("/tmp", "/workspace")  # noqa: S108 - 컨테이너 내부 경로


@dataclass(frozen=True)
class PortBinding:
    """A container port exposed to the runtime.

    노출 포트. Control 포트는 runtime 만, A2A 포트는 allowlist peer 만 접근한다.
    """

    name: str
    container_port: int


@dataclass(frozen=True)
class ContainerSpec:
    """Everything needed to create one agent container.

    에이전트 컨테이너 생성에 필요한 전부. Docker SDK 호출 인자로 그대로 옮겨진다.
    """

    name: str
    image: str
    env: Mapping[str, str]
    network: str
    ports: tuple[PortBinding, ...]
    cpu_cores: float
    memory_bytes: int
    pids_limit: int
    user: str
    read_only_rootfs: bool
    cap_drop: tuple[str, ...]
    tmpfs: tuple[str, ...]
    volumes: tuple[Mapping[str, Any], ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)

    def to_docker_kwargs(self) -> dict[str, Any]:
        """Render arguments for the Docker SDK.

        Docker SDK 의 컨테이너 생성 인자로 변환합니다.
        """
        return {
            "name": self.name,
            "image": self.image,
            "environment": dict(self.env),
            "network": self.network,
            "ports": {f"{p.container_port}/tcp": None for p in self.ports},
            "nano_cpus": int(self.cpu_cores * 1_000_000_000),
            "mem_limit": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "user": self.user,
            "read_only": self.read_only_rootfs,
            "cap_drop": list(self.cap_drop),
            "tmpfs": dict.fromkeys(self.tmpfs, ""),
            "labels": dict(self.labels),
            # 호스트 네트워크 금지, 임의 포트 publish 금지 (02 Network 규칙)
            "network_mode": None,
            "publish_all_ports": False,
        }


def build_container_spec(
    manifest: AgentManifest,
    *,
    env: Mapping[str, str] | None = None,
    replica: int = 0,
    network: str = DEFAULT_NETWORK,
    a2a_port: int | None = None,
    base_image: str = DEFAULT_BASE_IMAGE,
) -> ContainerSpec:
    """Derive a container spec from an agent manifest.

    Manifest 로부터 컨테이너 스펙을 도출합니다. 보안 기본값은 선언 여부와
    무관하게 항상 적용됩니다 — 빠뜨려서 취약해지는 경로를 없애기 위함입니다.

    Args:
        manifest: The validated agent manifest.
        env: Resolved secret values to inject (scope resolution happens earlier).
        replica: Replica index, used to make container names unique.
        network: Bridge network to attach.
        a2a_port: A2A port assigned by the runtime, when A2A is enabled.
        base_image: Image used when the manifest declares none.

    Returns:
        The container specification.
    """
    runtime = manifest.spec.runtime

    ports = [PortBinding("control", DEFAULT_CONTROL_PORT)]
    if manifest.spec.a2a.enabled and a2a_port is not None:
        ports.append(PortBinding("a2a", a2a_port))

    volumes = tuple(
        {
            "name": volume.name,
            "mount_path": volume.mount_path,
            "read_only": volume.read_only,
        }
        for volume in runtime.volumes
    )

    return ContainerSpec(
        name=container_name(manifest.name, replica),
        image=runtime.image or base_image,
        env=dict(env or {}),
        network=network,
        ports=tuple(ports),
        cpu_cores=runtime.resources.cpu_cores,
        memory_bytes=runtime.resources.memory_bytes,
        pids_limit=DEFAULT_PID_LIMIT,
        # base 이미지가 non-root 로 실행되지만, 스펙에서도 명시해 이미지 변경에
        # 관계없이 root 로 뜨지 않게 한다
        user="1000:1000",
        read_only_rootfs=True,
        cap_drop=("ALL",),
        tmpfs=_WRITABLE_TMPFS,
        volumes=volumes,
        labels={
            "malkuth.agent": manifest.name,
            "malkuth.agent_version": manifest.metadata.version,
            "malkuth.group": manifest.group,
            "malkuth.replica": str(replica),
        },
    )


def container_name(agent: str, replica: int = 0) -> str:
    """Build the container name for an agent replica.

    에이전트 레플리카의 컨테이너 이름을 만듭니다.
    """
    return f"malkuth-{agent}-{replica}"
