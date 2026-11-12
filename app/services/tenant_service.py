"""Tenant use cases.

Every state-changing call is one transaction that writes the tenant, its new task and
the outbox row together, so the event exists if and only if the change was committed.
"""

from datetime import datetime
from typing import NoReturn
from uuid import UUID, uuid4

from app.core.clock import Clock, utc_now
from app.core.pagination import Cursor, paginate
from app.db.database import Database
from app.db.repository import Repository
from app.domain.errors import (
    TenantNotFoundError,
    TenantUpdateNotAllowedError,
    TenantVersionConflictError,
)
from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.domain.rules import TASK_RULES, ensure_can_start
from app.messaging.events import TaskEvent


class TenantService:
    def __init__(self, db: Database, clock: Clock = utc_now) -> None:
        self._db = db
        self._clock = clock

    async def create(self, slug: str, name: str) -> tuple[Tenant, Task]:
        now = self._clock()
        tenant = Tenant(
            id=uuid4(),
            slug=slug,
            name=name,
            status=TASK_RULES[TaskType.DEPLOY].in_flight,
            version=1,
            created_at=now,
            updated_at=now,
        )
        async with self._db.transaction() as repo:
            await repo.insert_tenant(tenant)  # a taken slug raises TenantAlreadyExistsError
            task = await _start_task(repo, tenant, TaskType.DEPLOY, now)
        return tenant, task

    async def update(
        self, tenant_id: UUID, *, name: str | None, expected_version: int
    ) -> tuple[Tenant, Task]:
        return await self._transition(
            tenant_id, TaskType.UPDATE, name=name, expected_version=expected_version
        )

    async def delete(self, tenant_id: UUID) -> tuple[Tenant, Task]:
        return await self._transition(tenant_id, TaskType.DESTROY)

    async def get(self, tenant_id: UUID) -> Tenant:
        async with self._db.read() as repo:
            tenant = await repo.get_tenant(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(f"tenant {tenant_id} not found")
        return tenant

    async def list_tenants(
        self, *, status: TenantStatus | None, cursor: Cursor | None, limit: int
    ) -> tuple[list[Tenant], Cursor | None]:
        after = (cursor.created_at, cursor.id) if cursor else None
        async with self._db.read() as repo:
            rows = await repo.list_tenants(status=status, after=after, limit=limit + 1)
        return paginate(rows, limit)

    async def _transition(
        self,
        tenant_id: UUID,
        task_type: TaskType,
        *,
        name: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[Tenant, Task]:
        rule = TASK_RULES[task_type]
        now = self._clock()
        async with self._db.transaction() as repo:
            tenant = await repo.transition_tenant(
                tenant_id,
                new_status=rule.in_flight,
                allowed_from=rule.allowed_from,
                now=now,
                expected_version=expected_version,
                name=name,
            )
            if tenant is None:
                await _explain_rejection(repo, tenant_id, task_type, expected_version)
            task = await _start_task(repo, tenant, task_type, now)
        return tenant, task


async def _explain_rejection(
    repo: Repository, tenant_id: UUID, task_type: TaskType, expected_version: int | None
) -> NoReturn:
    """The guarded UPDATE matched no row; raise the error that says why."""
    current = await repo.get_tenant(tenant_id)
    if current is None:
        raise TenantNotFoundError(f"tenant {tenant_id} not found")
    if expected_version is not None and current.version != expected_version:
        raise TenantVersionConflictError(current.version, expected_version)
    ensure_can_start(task_type, current.status)
    # Every guard passes now, so the row changed between our UPDATE and this read.
    raise TenantUpdateNotAllowedError("tenant changed concurrently; retry")


async def _start_task(repo: Repository, tenant: Tenant, task_type: TaskType, now: datetime) -> Task:
    """Insert the task and its outbox event; the caller's transaction makes them atomic."""
    task = Task(
        id=uuid4(),
        tenant_id=tenant.id,
        type=task_type,
        status=TaskStatus.ACCEPTED,
        error=None,
        created_at=now,
        updated_at=now,
    )
    await repo.insert_task(task)
    event = TaskEvent.for_task(task, tenant)
    await repo.insert_outbox(
        event_id=task.id, routing_key=event.routing_key, payload=event.model_dump_json()
    )
    return task
