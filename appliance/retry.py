"""Shared retry timing.

Both the engine manager connection and the trunk registration layer retry on
failure.  Sharing one implementation means the backoff behaviour is verified
once and cannot drift between the two, and it keeps the timing policy pure and
therefore exactly testable.
"""

from __future__ import annotations

import random
from typing import Callable

__all__ = ["compute_backoff", "BackoffPolicy"]


def compute_backoff(
    attempt: int,
    base_seconds: float,
    ceiling_seconds: float,
    jitter_ratio: float = 0.25,
    random_fn: Callable[[], float] | None = None,
) -> float:
    """Return the delay before a given retry attempt.

    The delay doubles with each attempt until it reaches the ceiling, then
    stays there.  Jitter is applied symmetrically around the computed delay so
    that a population of trunks recovering from a common outage does not
    stampede the carrier in lockstep.  The result is never negative and never
    exceeds the ceiling raised by the jitter ratio.
    """
    if attempt < 1:
        raise ValueError("the attempt number starts at one")
    if base_seconds <= 0:
        raise ValueError("the base interval must be greater than zero")
    if ceiling_seconds < base_seconds:
        raise ValueError("the ceiling cannot be below the base interval")
    if not 0.0 <= jitter_ratio < 1.0:
        raise ValueError("the jitter ratio must fall between zero and one")

    # Cap the exponent before computing the power so that a long lived
    # failure cannot produce an overflow on a very large attempt count.
    doublings = min(attempt - 1, 32)
    delay = min(base_seconds * (2.0 ** doublings), ceiling_seconds)

    if jitter_ratio:
        draw = (random_fn or random.random)()
        # Map the draw onto the symmetric interval around the delay.
        offset = (draw * 2.0 - 1.0) * jitter_ratio * delay
        delay += offset

    return max(0.0, delay)


class BackoffPolicy:
    """A stateful retry schedule for one subject."""

    def __init__(
        self,
        base_seconds: float,
        ceiling_seconds: float,
        jitter_ratio: float = 0.25,
        random_fn: Callable[[], float] | None = None,
    ) -> None:
        self.base_seconds = base_seconds
        self.ceiling_seconds = ceiling_seconds
        self.jitter_ratio = jitter_ratio
        self._random_fn = random_fn
        self.attempt = 0

    def next_delay(self) -> float:
        self.attempt += 1
        return compute_backoff(
            self.attempt,
            self.base_seconds,
            self.ceiling_seconds,
            self.jitter_ratio,
            self._random_fn,
        )

    def reset(self) -> None:
        self.attempt = 0
