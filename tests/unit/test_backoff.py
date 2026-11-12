"""Unit tests for the retry delay."""

import pytest

from app.core.backoff import backoff_delay


def upper_bound(low: float, high: float) -> float:
    """Stands in for random.uniform: always the largest allowed delay."""
    return high


def test_ceiling_doubles_per_attempt() -> None:
    delays = [backoff_delay(n, base=0.5, cap=100, rand=upper_bound) for n in range(1, 5)]

    assert delays == [0.5, 1.0, 2.0, 4.0]


def test_ceiling_is_capped() -> None:
    assert backoff_delay(10, base=0.5, cap=30, rand=upper_bound) == 30


def test_huge_attempt_counts_do_not_overflow() -> None:
    assert backoff_delay(10_000, base=0.5, cap=30, rand=upper_bound) == 30


def test_delay_is_jittered_within_zero_and_the_ceiling() -> None:
    delays = [backoff_delay(3, base=0.5, cap=30) for _ in range(200)]

    assert all(0 <= delay <= 2.0 for delay in delays)
    assert len(set(delays)) > 1


def test_attempt_starts_at_one() -> None:
    with pytest.raises(ValueError, match="attempt starts at 1"):
        backoff_delay(0)
