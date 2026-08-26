"""Agent runtime — container lifecycle, control API, resource scoping.

에이전트 런타임. Docker SDK 를 직접 만지는 유일한 레이어이며,
오케스트레이터에게는 에이전트를 async callable 로 노출한다.
"""

from malkuth.runtime.quota import (
    ResourceTotals,
    check_group_quota,
    check_host_capacity,
    demand_of,
    total_demand,
)
from malkuth.runtime.scope import ResolvedSecret, ScopedSecrets, SecretScope

__all__ = [
    "ResolvedSecret",
    "ResourceTotals",
    "ScopedSecrets",
    "SecretScope",
    "check_group_quota",
    "check_host_capacity",
    "demand_of",
    "total_demand",
]
