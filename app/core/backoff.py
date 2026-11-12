"""Retry delays: exponential backoff with full jitter."""

import random
from collections.abc import Callable

# 2**20 * base is far beyond any sensible cap; bounding the exponent avoids float overflow.
_MAX_EXPONENT = 20


def backoff_delay(
    attempt: int,
    *,
    base: float = 0.5,
    cap: float = 30.0,
    rand: Callable[[float, float], float] = random.uniform,
) -> float:
    """Seconds to wait before retry number `attempt` (1 = first retry).

    The ceiling doubles per attempt (base, 2*base, 4*base, ...) up to `cap`; the actual
    delay is random between 0 and that ceiling, so many clients retrying at once spread
    out instead of hammering the broker in lockstep. Jitter, not security: plain
    `random` is fine.
    """
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(cap, base * 2 ** min(attempt - 1, _MAX_EXPONENT))
    return rand(0.0, ceiling)
