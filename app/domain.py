"""Domain rules: tenant and task state machines, and the errors the API exposes.

Pure logic with no I/O, so every rule can be unit-tested in isolation. The database
layer enforces the same rules with conditional UPDATEs, so a race cannot bypass them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Final

# k8s-namespace compatible, 3-28 characters (the pattern itself bounds the length).
SLUG_PATTERN: Final = re.compile(r"^[a-z][-a-z0-9]{1,26}[a-z0-9]$")


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
class TaskRule:
    """How one task type moves its tenant through the lifecycle.

    allowed_from: tenant statuses in which the API may start this task.
    in_flight:    tenant status while the task is open.
    on_done:      tenant status once the task succeeds. A failed task always
                  moves the tenant to FAILED.
    """

    allowed_from: frozenset[TenantStatus]
    in_flight: TenantStatus
    on_done: TenantStatus


# The whole tenant state machine in one table. A deploy task is only ever
# created together with a brand-new tenant, so no existing status allows it.
TASK_RULES: Final[Mapping[TaskType, TaskRule]] = MappingProxyType(
    {
        TaskType.DEPLOY: TaskRule(
            allowed_from=frozenset(),
            in_flight=TenantStatus.PROVISIONING,
            on_done=TenantStatus.ACTIVE,
        ),
        TaskType.UPDATE: TaskRule(
            allowed_from=frozenset({TenantStatus.ACTIVE}),
            in_flight=TenantStatus.UPDATING,
            on_done=TenantStatus.ACTIVE,
        ),
        TaskType.DESTROY: TaskRule(
            allowed_from=frozenset({TenantStatus.ACTIVE, TenantStatus.FAILED}),
            in_flight=TenantStatus.DESTROYING,
            on_done=TenantStatus.DESTROYED,
        ),
    }
)

# Tasks only move forward. Ranking terminal statuses equally means a terminal
# update can follow `accepted` directly: if the broker delivers `done` before
# `in_progress`, we keep the outcome and later ignore the stale `in_progress`.
_TASK_RANK: Final[Mapping[TaskStatus, int]] = MappingProxyType(
    {
        TaskStatus.ACCEPTED: 0,
        TaskStatus.IN_PROGRESS: 1,
        TaskStatus.DONE: 2,
        TaskStatus.FAILED: 2,
    }
)


def is_valid_slug(slug: str) -> bool:
    return SLUG_PATTERN.fullmatch(slug) is not None


def ensure_can_start(task_type: TaskType, tenant_status: TenantStatus) -> None:
    """Raise if the API may not start `task_type` on a tenant in `tenant_status`."""
    if tenant_status not in TASK_RULES[task_type].allowed_from:
        raise TenantUpdateNotAllowedError(
            f"cannot start a {task_type} task while the tenant is {tenant_status}"
        )


def can_apply_task_update(current: TaskStatus, new: TaskStatus) -> bool:
    """True if a worker update from `current` to `new` moves the task forward.

    False means the update is stale (duplicate, out of order, or for a finished
    task): the consumer acknowledges it and changes nothing.
    """
    if current.is_terminal:
        return False
    return _TASK_RANK[new] > _TASK_RANK[current]


def tenant_status_after(task_type: TaskType, outcome: TaskStatus) -> TenantStatus:
    """Tenant status once a task of `task_type` ends with `outcome`."""
    if not outcome.is_terminal:
        raise ValueError(f"{outcome} is not a terminal task status")
    if outcome is TaskStatus.FAILED:
        return TenantStatus.FAILED
    return TASK_RULES[task_type].on_done


class DomainError(Exception):
    """Base for errors the API turns into a structured response.

    `code` is part of the public API contract: clients branch on it.
    """

    code: ClassVar[str]

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TenantNotFoundError(DomainError):
    code = "tenant_not_found"


class TenantAlreadyExistsError(DomainError):
    code = "tenant_already_exists"


class TenantUpdateNotAllowedError(DomainError):
    code = "tenant_update_not_allowed"


class TenantVersionConflictError(DomainError):
    code = "tenant_version_conflict"

    def __init__(self, current_version: int, requested_version: int) -> None:
        super().__init__(f"tenant version is {current_version}, request had {requested_version}")
        self.current_version = current_version


class TaskNotFoundError(DomainError):
    code = "task_not_found"
