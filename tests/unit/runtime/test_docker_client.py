"""SdkDockerClient 의 순수 부분 — 이미지 참조 해석."""

from __future__ import annotations

import pytest

from malkuth.runtime.docker.client import image_reference


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("malkuth/agent-base:0.1.0", ("malkuth/agent-base", "0.1.0")),
        # registry 포트의 콜론은 태그가 아니다
        ("registry.local:5000/team/agent:1.2.3", ("registry.local:5000/team/agent", "1.2.3")),
        ("registry.local:5000/team/agent", ("registry.local:5000/team/agent", "latest")),
        # digest 는 태그 자리에 그대로 간다 — SDK 가 그렇게 당긴다
        ("agent@sha256:" + "a" * 64, ("agent", "sha256:" + "a" * 64)),
        ("agent", ("agent", "latest")),
    ],
)
def test_image_reference_splits_like_the_docker_cli(image, expected):
    assert image_reference(image) == expected
