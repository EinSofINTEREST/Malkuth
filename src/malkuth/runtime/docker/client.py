"""The production DockerClient, over the Docker SDK.

`DockerClient` 프로토콜의 구현체가 **테스트에만** 둘 있었다 — `CliDockerClient`(subprocess)
와 `FakeDockerClient`. `pyproject.toml` 의 `docker` 의존성은 선언만 있고 import 하는 곳이
없었다 (#243). 01 은 runtime/ 을 "Docker SDK 를 만지는 유일한 층" 으로 규정하는데, 아무
층도 만지지 않고 있었다 — 그래서 컨테이너를 띄우는 것은 compose 뿐이었다.

예외는 SDK 것을 그대로 올린다. `DockerEngine` 이 단계별로 `except Exception` 으로 받아
RT_004(이미지) / RT_001(기동) 로 옮기므로, 여기서 또 감싸면 이중이다 (05 Layer Rules).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import docker
from docker.errors import ImageNotFound, NotFound

if TYPE_CHECKING:
    from docker import DockerClient as SdkHandle

BRIDGE = "bridge"


class SdkDockerClient:
    """`DockerClient` over docker-py.

    docker-py 위의 `DockerClient` 구현. 메서드는 동기다 — `DockerEngine` 이
    `asyncio.to_thread` 로 감싼다 (07 Async 2).
    """

    def __init__(self, handle: SdkHandle | None = None) -> None:
        # 환경변수(DOCKER_HOST 등)로 데몬을 찾는다 — 소켓 경로를 하드코딩하지 않는다
        self._sdk = handle or docker.from_env()

    def ensure_image(self, image: str) -> None:
        """이미지를 확보한다 — 없으면 pull.

        에이전트 이미지는 배포 파이프라인이 굽는다 (02 Lifecycle 1) — 로컬 태그는
        pull 이 실패하고, 그 실패는 엔진이 RT_004 로 올린다.
        """
        try:
            self._sdk.images.get(image)
        except ImageNotFound:
            repository, _, tag = image.rpartition(":")
            self._sdk.images.pull(repository or image, tag=tag or None)

    def ensure_network(self, name: str) -> None:
        """네트워크를 확보한다 — 없으면 bridge 로 생성 (02 Network)."""
        try:
            self._sdk.networks.get(name)
        except NotFound:
            self._sdk.networks.create(name, driver=BRIDGE)

    def create(self, **kwargs: Any) -> str:
        """컨테이너를 만들고 id 를 돌려준다.

        `ContainerSpec.to_docker_kwargs()` 를 그대로 받는다.
        """
        container_id: str = self._sdk.containers.create(**kwargs).id
        return container_id

    def start(self, container_id: str) -> None:
        self._sdk.containers.get(container_id).start()

    def inspect(self, container_id: str) -> dict[str, Any]:
        """`.State` 조각 — CLI 구현(`docker inspect --format '{{json .State}}'`)과 같다."""
        container = self._sdk.containers.get(container_id)
        container.reload()
        state: dict[str, Any] = container.attrs["State"]
        return state

    def port_of(self, container_id: str, container_port: int) -> int:
        container = self._sdk.containers.get(container_id)
        container.reload()
        bindings = container.ports.get(f"{container_port}/tcp") or []
        if not bindings:
            raise LookupError(f"container port {container_port} is not published")
        return int(bindings[0]["HostPort"])

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        """SIGTERM 후 유예가 지나면 SIGKILL (02 Lifecycle 5)."""
        self._sdk.containers.get(container_id).stop(timeout=int(timeout_s))

    def remove(self, container_id: str) -> None:
        try:
            self._sdk.containers.get(container_id).remove(force=True)
        except NotFound:
            return  # 이미 없다 — 정리의 목적은 달성됐다


__all__ = ["SdkDockerClient"]
