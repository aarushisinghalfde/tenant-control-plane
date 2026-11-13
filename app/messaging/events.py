"""Message formats exchanged with the worker over the broker.

Both directions are versioned JSON (`schema_version`), so a format can change later
without breaking messages already queued.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, model_validator

from app.core.clock import Timestamp
from app.domain.models import Task, TaskStatus, TaskType, Tenant


class TenantRef(BaseModel):
    slug: str
    name: str


class TaskEvent(BaseModel):
    """Control plane -> worker. Published for every new task: the task *is* the event,
    so its id is the event id."""

    schema_version: Literal[1] = 1
    id: UUID
    type: TaskType
    tenant_id: UUID
    status: TaskStatus
    created_at: Timestamp
    tenant: TenantRef

    @classmethod
    def for_task(cls, task: Task, tenant: Tenant) -> TaskEvent:
        return cls(
            id=task.id,
            type=task.type,
            tenant_id=task.tenant_id,
            status=task.status,
            created_at=task.created_at,
            tenant=TenantRef(slug=tenant.slug, name=tenant.name),
        )

    @property
    def routing_key(self) -> str:
        return f"task.{self.type}"


# The statuses a worker may report; `accepted` is set only by the control plane.
UpdateStatus = Literal[TaskStatus.IN_PROGRESS, TaskStatus.DONE, TaskStatus.FAILED]


class TaskUpdate(BaseModel):
    """Worker -> control plane: one step of progress on one task.

    `message_id` is the idempotency key: the consumer applies each id at most once.
    `task_id` and `tenant_id` say exactly what the update is about.
    """

    schema_version: Literal[1] = 1
    message_id: UUID
    task_id: UUID
    tenant_id: UUID
    status: UpdateStatus
    error: str | None = None
    occurred_at: Timestamp

    @model_validator(mode="after")
    def _error_only_when_failed(self) -> Self:
        if self.status is TaskStatus.FAILED and not self.error:
            raise ValueError("a failed update must say why in `error`")
        if self.status is not TaskStatus.FAILED and self.error is not None:
            raise ValueError("only a failed update carries an `error`")
        return self

    @classmethod
    def for_task(
        cls,
        event: TaskEvent,
        status: UpdateStatus,
        *,
        occurred_at: datetime,
        error: str | None = None,
    ) -> TaskUpdate:
        return cls(
            # Derived, not random: the same step of the same task always gets the same
            # id, so if the worker crashes and redoes a task, the consumer still sees
            # the repeat as a duplicate.
            message_id=uuid5(event.id, status.value),
            task_id=event.id,
            tenant_id=event.tenant_id,
            status=status,
            error=error,
            occurred_at=occurred_at,
        )

    @property
    def routing_key(self) -> str:
        return f"task.update.{self.status}"
