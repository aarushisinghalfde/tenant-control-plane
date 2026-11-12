"""Unit tests for the message published to the worker."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.messaging.events import TaskEvent


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
