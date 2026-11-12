"""Unit tests for the tenant and task state machines.

Every combination is enumerated, so both allowed and forbidden transitions are covered.
"""

import pytest

from app.domain import (
    TaskNotFoundError,
    TaskStatus,
    TaskType,
    TenantAlreadyExistsError,
    TenantNotFoundError,
    TenantStatus,
    TenantUpdateNotAllowedError,
    TenantVersionConflictError,
    can_apply_task_update,
    ensure_can_start,
    is_valid_slug,
    tenant_status_after,
)

# --- slug ------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["abc", "acme-corp", "a1b", "tenant-42", "a" + "b" * 26 + "c"],  # last one: 28 chars
)
def test_valid_slugs(slug: str) -> None:
    assert is_valid_slug(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "ab",  # too short
        "a" + "b" * 27 + "c",  # 29 chars, too long
        "1abc",  # must start with a letter
        "-abc",  # must start with a letter
        "abc-",  # must end with a letter or digit
        "Acme",  # uppercase not allowed
        "acme_corp",  # underscore not allowed
        "acme corp",  # space not allowed
        "acme\n",  # trailing newline must not sneak past the anchor
        "",
    ],
)
def test_invalid_slugs(slug: str) -> None:
    assert not is_valid_slug(slug)


# --- API-driven tenant transitions ------------------------------------------

ALLOWED_STARTS = {
    (TaskType.UPDATE, TenantStatus.ACTIVE),
    (TaskType.DESTROY, TenantStatus.ACTIVE),
    (TaskType.DESTROY, TenantStatus.FAILED),
}


@pytest.mark.parametrize("tenant_status", list(TenantStatus))
@pytest.mark.parametrize("task_type", list(TaskType))
def test_ensure_can_start_covers_every_status(
    task_type: TaskType, tenant_status: TenantStatus
) -> None:
    if (task_type, tenant_status) in ALLOWED_STARTS:
        ensure_can_start(task_type, tenant_status)  # must not raise
    else:
        with pytest.raises(TenantUpdateNotAllowedError):
            ensure_can_start(task_type, tenant_status)


# --- worker-driven tenant transitions ---------------------------------------


@pytest.mark.parametrize(
    ("task_type", "outcome", "expected"),
    [
        (TaskType.DEPLOY, TaskStatus.DONE, TenantStatus.ACTIVE),
        (TaskType.DEPLOY, TaskStatus.FAILED, TenantStatus.FAILED),
        (TaskType.UPDATE, TaskStatus.DONE, TenantStatus.ACTIVE),
        (TaskType.UPDATE, TaskStatus.FAILED, TenantStatus.FAILED),
        (TaskType.DESTROY, TaskStatus.DONE, TenantStatus.DESTROYED),
        (TaskType.DESTROY, TaskStatus.FAILED, TenantStatus.FAILED),
    ],
)
def test_tenant_status_after_terminal_outcome(
    task_type: TaskType, outcome: TaskStatus, expected: TenantStatus
) -> None:
    assert tenant_status_after(task_type, outcome) is expected


@pytest.mark.parametrize("outcome", [TaskStatus.ACCEPTED, TaskStatus.IN_PROGRESS])
def test_tenant_status_after_rejects_non_terminal_outcome(outcome: TaskStatus) -> None:
    with pytest.raises(ValueError, match="not a terminal"):
        tenant_status_after(TaskType.DEPLOY, outcome)


# --- task transitions (forward-only) ----------------------------------------

FORWARD_UPDATES = {
    (TaskStatus.ACCEPTED, TaskStatus.IN_PROGRESS),
    (TaskStatus.ACCEPTED, TaskStatus.DONE),  # terminal update overtook in_progress
    (TaskStatus.ACCEPTED, TaskStatus.FAILED),
    (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED),
}


@pytest.mark.parametrize("new", list(TaskStatus))
@pytest.mark.parametrize("current", list(TaskStatus))
def test_task_updates_only_move_forward(current: TaskStatus, new: TaskStatus) -> None:
    assert can_apply_task_update(current, new) is ((current, new) in FORWARD_UPDATES)


def test_stale_in_progress_after_done_is_ignored() -> None:
    assert not can_apply_task_update(TaskStatus.DONE, TaskStatus.IN_PROGRESS)


def test_terminal_statuses() -> None:
    assert {s for s in TaskStatus if s.is_terminal} == {TaskStatus.DONE, TaskStatus.FAILED}


# --- error contract -----------------------------------------------------------


def test_error_codes_match_the_api_contract() -> None:
    assert TenantNotFoundError.code == "tenant_not_found"
    assert TenantAlreadyExistsError.code == "tenant_already_exists"
    assert TenantUpdateNotAllowedError.code == "tenant_update_not_allowed"
    assert TenantVersionConflictError.code == "tenant_version_conflict"
    assert TaskNotFoundError.code == "task_not_found"


def test_version_conflict_carries_current_version() -> None:
    error = TenantVersionConflictError(current_version=3, requested_version=2)
    assert error.current_version == 3
    assert error.message == "tenant version is 3, request had 2"
