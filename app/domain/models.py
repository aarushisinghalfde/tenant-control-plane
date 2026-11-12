"""Domain entities and their statuses."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class TenantStatus(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    UPDATING = "updating"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    FAILED = "failed"


class TaskType(StrEnum):
    DEPLOY = "deploy"
    UPDATE = "update"
    DESTROY = "destroy"


class TaskStatus(StrEnum):
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED)


@dataclass(frozen=True, slots=True)
class Tenant:
    """A customer environment. `version` increments on every change (optimistic locking)."""

    id: UUID
    slug: str
    name: str
    status: TenantStatus
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Task:
    """An asynchronous operation on a tenant; also the event published to the worker."""

    id: UUID
    tenant_id: UUID
    type: TaskType
    status: TaskStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
