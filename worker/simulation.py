"""What the worker will send for one task, decided up front.

Pure logic with no I/O, so every scenario (failure, duplicates, reordering) is
unit-tested without a broker.
"""

import random
from dataclasses import dataclass

from app.domain.models import TaskStatus
from app.messaging.events import UpdateStatus
from worker.config import WorkerConfig

FAILURE_REASON = "simulated provisioning failure"


@dataclass(frozen=True, slots=True)
class Step:
    """Wait `delay` seconds, then send `status`; a duplicate resends the previous message."""

    delay: float
    status: UpdateStatus
    duplicate: bool = False


def plan_steps(config: WorkerConfig, rng: random.Random) -> list[Step]:
    """Normally: in_progress, then done or failed, each after a random delay.

    `fail_rate` picks the outcome; `reorder` sends the outcome first and a stale
    in_progress after it; `duplicate_rate` may send any update a second time at once.
    """
    outcome: UpdateStatus = (
        TaskStatus.FAILED if rng.random() < config.fail_rate else TaskStatus.DONE
    )
    statuses: list[UpdateStatus] = [TaskStatus.IN_PROGRESS, outcome]
    if config.reorder:
        statuses.reverse()

    steps: list[Step] = []
    for status in statuses:
        delay_ms = rng.uniform(config.min_delay_ms, config.max_delay_ms)
        steps.append(Step(delay=delay_ms / 1000, status=status))
        if rng.random() < config.duplicate_rate:
            steps.append(Step(delay=0.0, status=status, duplicate=True))
    return steps
