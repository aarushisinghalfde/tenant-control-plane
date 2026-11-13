"""Applying the worker's progress updates to tasks and tenants.

Every update is applied in one transaction that also records its `message_id`, so
an update is applied at most once however often the broker delivers it.
"""

import logging
from enum import StrEnum

from app.core.clock import Clock, utc_now
from app.db.database import Database
from app.domain.rules import TASK_RULES, can_apply_task_update, tenant_status_after
from app.messaging.events import TaskUpdate

logger = logging.getLogger(__name__)


class UpdateOutcome(StrEnum):
    APPLIED = "applied"  # the task (and on a terminal outcome, the tenant) changed
    DUPLICATE = "duplicate"  # this message_id was applied before; nothing changed
    STALE = "stale"  # would move the task backwards or past its end; nothing changed


class UnusableUpdateError(Exception):
    """The update can never be applied (unknown task, wrong tenant); retrying won't help."""


class TaskUpdateService:
    def __init__(self, db: Database, clock: Clock = utc_now) -> None:
        self._db = db
        self._clock = clock

    async def apply(self, update: TaskUpdate) -> UpdateOutcome:
        """Apply one update; raises UnusableUpdateError for an update that can never apply.

        Raising rolls the whole transaction back, including the idempotency record, so a
        failed attempt leaves no trace and can be retried.
        """
        now = self._clock()
        async with self._db.transaction() as repo:
            if not await repo.record_processed_message(update.message_id, update.task_id):
                return UpdateOutcome.DUPLICATE

            task = await repo.lock_task(update.task_id)
            if task is None:
                raise UnusableUpdateError(f"unknown task {update.task_id}")
            if task.tenant_id != update.tenant_id:
                raise UnusableUpdateError(
                    f"task {task.id} belongs to tenant {task.tenant_id}, not {update.tenant_id}"
                )

            if not can_apply_task_update(task.status, update.status):
                # Keep the idempotency record (the transaction commits): this exact
                # message is settled, and a redelivery will be a cheap duplicate.
                return UpdateOutcome.STALE

            await repo.update_task_status(
                task.id, status=update.status, error=update.error, now=now
            )
            if update.status.is_terminal:
                rule = TASK_RULES[task.type]
                tenant = await repo.transition_tenant(
                    task.tenant_id,
                    new_status=tenant_status_after(task.type, update.status),
                    allowed_from=[rule.in_flight],
                    now=now,
                )
                if tenant is None:
                    # Cannot happen while the API's guards hold (a tenant with an open
                    # task stays in that task's in-flight status); log it loudly.
                    logger.error(
                        "task %s finished but tenant %s was not %s; tenant left unchanged",
                        task.id,
                        task.tenant_id,
                        rule.in_flight,
                    )
        return UpdateOutcome.APPLIED
