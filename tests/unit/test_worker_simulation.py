"""Unit tests for the worker's plan: outcome, order, duplicates and delays."""

import random
from dataclasses import replace

from app.domain.models import TaskStatus
from worker.config import WorkerConfig
from worker.simulation import Step, plan_steps

BASE = WorkerConfig(
    rabbitmq_url="amqp://unused",
    fail_rate=0.0,
    min_delay_ms=100,
    max_delay_ms=300,
    duplicate_rate=0.0,
    reorder=False,
    seed=None,
    concurrency=1,
    log_level="INFO",
)


def statuses(steps: list[Step]) -> list[str]:
    return [step.status for step in steps]


def test_happy_path_is_in_progress_then_done() -> None:
    steps = plan_steps(BASE, random.Random(1))

    assert statuses(steps) == ["in_progress", "done"]
    assert not any(step.duplicate for step in steps)


def test_fail_rate_one_always_fails() -> None:
    rng = random.Random(1)
    config = replace(BASE, fail_rate=1.0)

    outcomes = {plan_steps(config, rng)[-1].status for _ in range(50)}

    assert outcomes == {TaskStatus.FAILED}


def test_fail_rate_zero_never_fails() -> None:
    rng = random.Random(1)

    outcomes = {plan_steps(BASE, rng)[-1].status for _ in range(50)}

    assert outcomes == {TaskStatus.DONE}


def test_reorder_sends_the_outcome_first_then_a_stale_in_progress() -> None:
    steps = plan_steps(replace(BASE, reorder=True), random.Random(1))

    assert statuses(steps) == ["done", "in_progress"]


def test_duplicate_rate_one_sends_every_update_twice() -> None:
    steps = plan_steps(replace(BASE, duplicate_rate=1.0), random.Random(1))

    assert statuses(steps) == ["in_progress", "in_progress", "done", "done"]
    assert [step.duplicate for step in steps] == [False, True, False, True]
    assert steps[1].delay == 0.0


def test_delays_stay_within_bounds() -> None:
    rng = random.Random(1)

    delays = [step.delay for _ in range(50) for step in plan_steps(BASE, rng)]

    assert all(0.1 <= delay <= 0.3 for delay in delays)


def test_same_seed_gives_the_same_plan() -> None:
    config = replace(BASE, fail_rate=0.5, duplicate_rate=0.5)

    assert plan_steps(config, random.Random(7)) == plan_steps(config, random.Random(7))
