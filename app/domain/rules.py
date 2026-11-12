"""The tenant and task state machines.

Pure logic, so every rule is unit-tested in isolation. The repository enforces the
same rules with conditional UPDATEs, so a race cannot bypass them.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain.errors import TenantUpdateNotAllowedError
from app.domain.models import TaskStatus, TaskType, TenantStatus

# k8s-namespace compatible, 3-28 characters (the pattern itself bounds the length).
SLUG_PATTERN: Final = re.compile(r"^[a-z][-a-z0-9]{1,26}[a-z0-9]$")


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
    allowed_from = TASK_RULES[task_type].allowed_from
    if tenant_status in allowed_from:
        return
    if not allowed_from:
        raise TenantUpdateNotAllowedError(f"{task_type} runs only for new tenants")
    allowed = " or ".join(sorted(allowed_from))
    raise TenantUpdateNotAllowedError(
        f"cannot {task_type} tenant in status {tenant_status}; requires {allowed}"
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
