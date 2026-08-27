"""Executes retry policies.

재시도 정책을 **실행**한다.

`core.RetryPolicy` 는 판정(`should_retry`)과 백오프 계산(`delay_for`)까지
갖췄지만 순수하다 — `core` 는 I/O 를 하지 않기 때문이다 (01). 그것을 실제로
돌리는 곳이 여기다.

05 Retry Rules 1 이 수제 루프를 금지하므로 tenacity 위에 얹는다.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import structlog
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from malkuth.observability.logging import LogField

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from malkuth.core.errors import RetryPolicy

log = structlog.get_logger(__name__)

JITTER_RATIO = 0.25
"""대기 시간에 얹는 흔들림 비율 — 여러 호출자가 같은 순간에 몰려 재시도하면
백오프가 무의미해진다 (thundering herd)."""


def _jittered(delay: float, *, rng: random.Random | None = None) -> float:
    """Spread retries so they do not resynchronize.

    `delay_for` 는 **의도적으로 결정적**이다 — 테스트가 clock/random 주입
    없이 계산을 검증할 수 있게 하기 위함이다. 흔들림은 호출자 몫이고,
    실행하는 이 계층이 그 호출자다.
    """
    source = rng or random
    return delay * (1.0 - JITTER_RATIO * source.random())


async def retrying[T](
    policy: RetryPolicy,
    fn: Callable[[], Awaitable[T]],
    *,
    rng: random.Random | None = None,
    **context: object,
) -> T:
    """Run ``fn`` under ``policy``, retrying what the policy allows.

    정책이 허용하는 실패만 재시도하며 ``fn`` 을 실행합니다.

    Args:
        policy: The policy deciding what is retryable and how long to wait.
        fn: The awaitable operation — 재시도마다 **다시 호출**되므로
            부수효과가 있다면 멱등해야 합니다 (05 Retry Rules 5).
        rng: Injectable randomness for the jitter, for deterministic tests.
        context: Standard log fields (``agent``, ``task_id`` 등) merged into
            the retry warning.

    Returns:
        Whatever ``fn`` returns on the first successful attempt.

    Raises:
        BaseException: The last failure, once attempts are exhausted — 원본을
            그대로 올린다. 여기서 감싸면 호출자의 카테고리 기반 처리가
            깨진다.
        asyncio.CancelledError: Immediately, even while waiting — tenacity 의
            대기는 `asyncio.sleep` 이므로 취소가 그대로 통과한다
            (05 Retry Rules 3).
    """

    def _warn(state: RetryCallState) -> None:
        outcome = state.outcome
        if outcome is None or not outcome.failed:
            return
        log.warning(
            "operation failed, retrying",
            **{
                LogField.ATTEMPT: state.attempt_number,
                LogField.MAX_ATTEMPTS: policy.max_attempts,
                LogField.DELAY_MS: round((state.idle_for or 0.0) * 1000),
            },
            **context,
            exc_info=outcome.exception(),
        )

    def _wait(state: RetryCallState) -> float:
        return _jittered(policy.delay_for(state.attempt_number), rng=rng)

    # `should_retry` 를 **그대로** 쓴다 — 조건을 여기 다시 쓰면 정책과
    # 실행이 어긋나고, retryable=False 즉시 중단(05 Rules 2)이 조용히 깨진다
    attempts = AsyncRetrying(
        stop=stop_after_attempt(policy.max_attempts),
        wait=_wait,
        retry=retry_if_exception(policy.should_retry),
        before_sleep=_warn,
        reraise=True,
    )

    async for attempt in attempts:
        with attempt:
            return await fn()

    raise AssertionError("unreachable: tenacity always resolves an attempt")


__all__ = ["retrying"]
