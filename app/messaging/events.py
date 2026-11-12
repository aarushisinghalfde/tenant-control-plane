"""Message formats exchanged with the worker over the broker."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.core.clock import Timestamp
from app.domain.models import Task, TaskStatus, TaskType, Tenant


class TenantRef(BaseModel):
    slug: str
    name: str


class TaskEvent(BaseModel):
    """Published for every new task: the task *is* the event, so its id is the event id."""

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
