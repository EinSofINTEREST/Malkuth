"""Fake Docker client.

실제 daemon 없이 SDK 호출부의 에러 변환과 정리 동작을 검증한다.
"""

from __future__ import annotations

from typing import Any


class FakeDockerClient:
    """스크립트된 Docker 동작을 돌려주는 대역."""

    def __init__(
        self,
        *,
        image_error: Exception | None = None,
        network_error: Exception | None = None,
        create_error: Exception | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        remove_error: Exception | None = None,
        state: dict[str, Any] | None = None,
        host_port: int = 49152,
    ) -> None:
        self._image_error = image_error
        self._network_error = network_error
        self._create_error = create_error
        self._start_error = start_error
        self._stop_error = stop_error
        self._remove_error = remove_error
        self._state = state or {"Running": True, "ExitCode": 0, "OOMKilled": False}
        self._host_port = host_port

        self.images: list[str] = []
        self.networks: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.stopped: list[tuple[str, float]] = []
        self.removed: list[str] = []

    def ensure_image(self, image: str) -> None:
        if self._image_error is not None:
            raise self._image_error
        self.images.append(image)

    def ensure_network(self, name: str) -> None:
        if self._network_error is not None:
            raise self._network_error
        self.networks.append(name)

    def create(self, **kwargs: Any) -> str:
        if self._create_error is not None:
            raise self._create_error
        self.created.append(kwargs)
        return f"container-{len(self.created):04d}" + "0" * 20

    def start(self, container_id: str) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started.append(container_id)

    def inspect(self, container_id: str) -> dict[str, Any]:
        return dict(self._state)

    def port_of(self, container_id: str, container_port: int) -> int:
        return self._host_port

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        if self._stop_error is not None:
            raise self._stop_error
        self.stopped.append((container_id, timeout_s))

    def remove(self, container_id: str) -> None:
        if self._remove_error is not None:
            raise self._remove_error
        self.removed.append(container_id)


__all__ = ["FakeDockerClient"]
