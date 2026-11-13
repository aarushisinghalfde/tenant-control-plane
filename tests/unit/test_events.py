"""Unit tests for the messages exchanged with the worker."""

import json
from datetime import UTC, datetime
from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from app.core.clock import utc_now
from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.messaging.events import TaskEvent, TaskUpdate


def test_task_event_carries_what_the_worker_needs() -> None:
    now = datetime(2026, 11, 11, 10, 0, 0, 123000, tzinfo=UTC)
    tenant = Tenant(
        id=uuid4(),
        slug="acme",
        name="Acme",
        status=TenantStatus.PROVISIONING,
        version=1,
        created_at=now,
        updated_at=now,
    )
    task = Task(
        id=uuid4(),
        tenant_id=tenant.id,
        type=TaskType.DEPLOY,
        status=TaskStatus.ACCEPTED,
        error=None,
        created_at=now,
        updated_at=now,
    )

    event = TaskEvent.for_task(task, tenant)
    payload = json.loads(event.model_dump_json())

    assert event.routing_key == "task.deploy"
    assert payload == {
        "schema_version": 1,
        "id": str(task.id),
        "type": "deploy",
        "tenant_id": str(tenant.id),
        "status": "accepted",
        "created_at": "2026-11-11T10:00:00.123Z",
        "tenant": {"slug": "acme", "name": "Acme"},
    }


# --- TaskUpdate: worker -> control plane --------------------------------------------


def _event() -> TaskEvent:
    now = datetime(2026, 11, 11, 10, 0, 0, 123000, tzinfo=UTC)
    tenant = Tenant(
        id=uuid4(),
        slug="acme",
        name="Acme",
        status=TenantStatus.PROVISIONING,
        version=1,
        created_at=now,
        updated_at=now,
    )
    task = Task(
        id=uuid4(),
        tenant_id=tenant.id,
        type=TaskType.DEPLOY,
        status=TaskStatus.ACCEPTED,
        error=None,
        created_at=now,
        updated_at=now,
    )
    return TaskEvent.for_task(task, tenant)


def test_task_update_json_shape() -> None:
    event = _event()
    at = datetime(2026, 11, 11, 10, 0, 1, 456000, tzinfo=UTC)

    update = TaskUpdate.for_task(event, TaskStatus.IN_PROGRESS, occurred_at=at)

    assert update.routing_key == "task.update.in_progress"
    assert json.loads(update.model_dump_json()) == {
        "schema_version": 1,
        "message_id": str(uuid5(event.id, "in_progress")),
        "task_id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "status": "in_progress",
        "error": None,
        "occurred_at": "2026-11-11T10:00:01.456Z",
    }


def test_task_update_round_trips_through_json() -> None:
    update = TaskUpdate.for_task(
        _event(), TaskStatus.FAILED, occurred_at=utc_now(), error="disk full"
    )

    assert TaskUpdate.model_validate_json(update.model_dump_json()) == update


@pytest.mark.parametrize(
    ("status", "error"),
    [("failed", None), ("failed", ""), ("done", "oops"), ("in_progress", "oops")],
)
def test_error_is_required_exactly_when_failed(status: str, error: str | None) -> None:
    event = _event()
    body = {
        "message_id": str(uuid4()),
        "task_id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "status": status,
        "error": error,
        "occurred_at": "2026-11-11T10:00:01.456Z",
    }

    with pytest.raises(ValidationError):
        TaskUpdate.model_validate(body)


@pytest.mark.parametrize("status", ["accepted", "bogus"])
def test_worker_cannot_report_other_statuses(status: str) -> None:
    event = _event()
    body = {
        "message_id": str(uuid4()),
        "task_id": str(event.id),
        "tenant_id": str(event.tenant_id),
        "status": status,
        "occurred_at": "2026-11-11T10:00:01.456Z",
    }

    with pytest.raises(ValidationError):
        TaskUpdate.model_validate(body)
