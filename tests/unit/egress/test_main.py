"""Egress proxy process settings (#293)."""

from __future__ import annotations

import pytest

from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.egress.__main__ import settings
from malkuth.egress.connect import EgressMode

REGISTRY = {"MALKUTH_ACCESS_URL": "http://control-plane:8700", "MALKUTH_ACCESS_ENFORCER_TOKEN": "e"}


def test_the_proxy_does_not_start_without_the_registry():
    """판정 없이 뜨는 이그레스 프록시는 열린 문이다."""
    with pytest.raises(MalkuthError) as exc_info:
        settings({"MALKUTH_ACCESS_URL": "http://control-plane:8700"})

    assert exc_info.value.code == ErrorCode.CFG_001


def test_enforce_is_the_default_and_an_unknown_mode_is_refused():
    assert settings(REGISTRY)["mode"] is EgressMode.ENFORCE
    assert settings({**REGISTRY, "MALKUTH_EGRESS_MODE": "record"})["mode"] is EgressMode.RECORD
    with pytest.raises(MalkuthError):
        settings({**REGISTRY, "MALKUTH_EGRESS_MODE": "off"})


def test_private_destinations_are_an_explicit_list():
    found = settings(
        {**REGISTRY, "MALKUTH_EGRESS_PRIVATE_DESTINATIONS": " fake-provider:8000, ,lab:9"}
    )

    assert found["private_destinations"] == frozenset({"fake-provider:8000", "lab:9"})
    assert settings(REGISTRY)["private_destinations"] == frozenset()
