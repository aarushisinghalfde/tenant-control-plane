"""Unit tests for applying worker updates, with an in-memory store.

The real SQL (row locks, ON CONFLICT) is covered by the integration tests; these pin
down the decisions: dedupe, forward-only, tenant transitions, unusable updates.
"""

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.db.database import Database
from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.messaging.events import TaskEvent, TaskUpdate, UpdateStatus
from app.services.task_update_service import (
    TaskUpdateService,
    UnusableUpdateError,
    UpdateOutcome,
)

T0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 13, 10, 0, 5, tzinfo=UTC)


class FakeStore:
    """Plays the Database and the Repository the service uses."""

    def __init__(self) -> None:
        self.tenants: dict[UUID, Tenant] = {}
        self.tasks: dict[UUID, Task] = {}
        self.processed: set[UUID] = set()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["FakeStore"]:
        yield self

    async def record_processed_message(self, message_id: UUID, task_id: UUID) -> bool:
        if message_id in self.processed:
            return False
        self.processed.add(message_id)
        return True

    async def lock_task(self, task_id: UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def update_task_status(
        self, task_id: UUID, *, status: TaskStatus, error: str | None, now: datetime
    ) -> None:
        self.tasks[task_id] = replace(
            self.tasks[task_id], status=status, error=error, updated_at=now
        )

    async def transition_tenant(
        self,
        tenant_id: UUID,
        *,
        new_status: TenantStatus,
        allowed_from: Collection[TenantStatus],
        now: datetime,
        expected_version: int | None = None,
        name: str | None = None,
    ) -> Tenant | None:
        tenant = self.tenants[tenant_id]
        if tenant.status not in allowed_from:
            return None
        updated = replace(tenant, status=new_status, version=tenant.version + 1, updated_at=now)
        self.tenants[tenant_id] = updated
        return updated

    def add(self, task_type: TaskType, tenant_status: TenantStatus) -> tuple[Tenant, Task]:
        tenant = Tenant(
            id=uuid4(),
            slug="acme",
            name="Acme",
            status=tenant_status,
            version=1,
            created_at=T0,
            updated_at=T0,
        )
        task = Task(
            id=uuid4(),
            tenant_id=tenant.id,
            type=task_type,
            status=TaskStatus.ACCEPTED,
            error=None,
            created_at=T0,
            updated_at=T0,
        )
        self.tenants[tenant.id] = tenant
        self.tasks[task.id] = task
        return tenant, task


def make_update(
    tenant: Tenant, task: Task, status: UpdateStatus, error: str | None = None
) -> TaskUpdate:
    event = TaskEvent.for_task(task, tenant)
    return TaskUpdate.for_task(event, status, occurred_at=T0, error=error)


def service(store: FakeStore) -> TaskUpdateService:
    return TaskUpdateService(cast(Database, store), clock=lambda: LATER)


async def test_in_progress_moves_the_task_but_not_the_tenant() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)

    outcome = await service(store).apply(make_update(tenant, task, TaskStatus.IN_PROGRESS))

    assert outcome is UpdateOutcome.APPLIED
    assert store.tasks[task.id].status is TaskStatus.IN_PROGRESS
    assert store.tasks[task.id].updated_at == LATER
    assert store.tenants[tenant.id] == tenant  # untouched, version still 1


@pytest.mark.parametrize(
    ("task_type", "in_flight", "outcome", "tenant_after"),
    [
        (TaskType.DEPLOY, TenantStatus.PROVISIONING, TaskStatus.DONE, TenantStatus.ACTIVE),
        (TaskType.UPDATE, TenantStatus.UPDATING, TaskStatus.DONE, TenantStatus.ACTIVE),
        (TaskType.DESTROY, TenantStatus.DESTROYING, TaskStatus.DONE, TenantStatus.DESTROYED),
        (TaskType.DEPLOY, TenantStatus.PROVISIONING, TaskStatus.FAILED, TenantStatus.FAILED),
        (TaskType.UPDATE, TenantStatus.UPDATING, TaskStatus.FAILED, TenantStatus.FAILED),
        (TaskType.DESTROY, TenantStatus.DESTROYING, TaskStatus.FAILED, TenantStatus.FAILED),
    ],
)
async def test_terminal_outcome_moves_the_tenant_and_bumps_its_version(
    task_type: TaskType,
    in_flight: TenantStatus,
    outcome: UpdateStatus,
    tenant_after: TenantStatus,
) -> None:
    store = FakeStore()
    tenant, task = store.add(task_type, in_flight)
    error = "boom" if outcome is TaskStatus.FAILED else None

    result = await service(store).apply(make_update(tenant, task, outcome, error))

    assert result is UpdateOutcome.APPLIED
    assert store.tasks[task.id].status is outcome
    assert store.tasks[task.id].error == error
    assert store.tenants[tenant.id].status is tenant_after
    assert store.tenants[tenant.id].version == 2


async def test_same_update_twice_changes_things_once() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)
    done = make_update(tenant, task, TaskStatus.DONE)

    first = await service(store).apply(done)
    second = await service(store).apply(done)

    assert (first, second) == (UpdateOutcome.APPLIED, UpdateOutcome.DUPLICATE)
    assert store.tenants[tenant.id].version == 2  # bumped once, not twice


async def test_done_before_in_progress_keeps_done() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)

    await service(store).apply(make_update(tenant, task, TaskStatus.DONE))
    late = await service(store).apply(make_update(tenant, task, TaskStatus.IN_PROGRESS))

    assert late is UpdateOutcome.STALE
    assert store.tasks[task.id].status is TaskStatus.DONE
    assert store.tenants[tenant.id].status is TenantStatus.ACTIVE
    assert store.tenants[tenant.id].version == 2


async def test_stale_update_is_still_recorded_so_a_redelivery_is_a_duplicate() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)
    late = make_update(tenant, task, TaskStatus.IN_PROGRESS)
    await service(store).apply(make_update(tenant, task, TaskStatus.DONE))

    assert await service(store).apply(late) is UpdateOutcome.STALE
    assert await service(store).apply(late) is UpdateOutcome.DUPLICATE


async def test_no_update_moves_a_finished_task() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)
    await service(store).apply(make_update(tenant, task, TaskStatus.FAILED, "boom"))

    # A different terminal outcome for the same task (different message_id).
    result = await service(store).apply(make_update(tenant, task, TaskStatus.DONE))

    assert result is UpdateOutcome.STALE
    assert store.tasks[task.id].status is TaskStatus.FAILED
    assert store.tenants[tenant.id].status is TenantStatus.FAILED


async def test_unknown_task_is_unusable() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)
    ghost = replace(task, id=uuid4())

    with pytest.raises(UnusableUpdateError, match="unknown task"):
        await service(store).apply(make_update(tenant, ghost, TaskStatus.DONE))


async def test_update_naming_the_wrong_tenant_is_unusable() -> None:
    store = FakeStore()
    tenant, task = store.add(TaskType.DEPLOY, TenantStatus.PROVISIONING)
    wrong = make_update(tenant, task, TaskStatus.DONE).model_copy(update={"tenant_id": uuid4()})

    with pytest.raises(UnusableUpdateError, match="belongs to tenant"):
        await service(store).apply(wrong)

    assert store.tenants[tenant.id].status is TenantStatus.PROVISIONING
