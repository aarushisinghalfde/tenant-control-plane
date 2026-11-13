"""Outbox and consumer behaviour against the real broker and database.

Consumer tests publish hand-made updates straight to the `task-updates` exchange, so
they control order and duplication exactly instead of relying on the worker's timing.
"""

import json
from uuid import UUID, uuid4

import asyncpg
import httpx2

from tests.integration.support import (
    create_tenant,
    dead_letter_count,
    eventually,
    publish_update,
    wait_for_status,
)

PROCESSED_FOR_TASK = "SELECT count(*) FROM processed_messages WHERE task_id = $1"


async def test_every_task_is_published_exactly_once_with_its_fields(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    _, task = await create_tenant(api)

    row = await eventually(
        lambda: db.fetchrow(
            "SELECT published_at, payload FROM outbox WHERE event_id = $1", UUID(task["id"])
        ),
        lambda r: r is not None and r["published_at"] is not None,
        what="the outbox relay to publish the task",
    )

    event = json.loads(row["payload"])
    assert {k: event[k] for k in ("id", "type", "tenant_id", "status")} == {
        k: task[k] for k in ("id", "type", "tenant_id", "status")
    }
    rows = "SELECT count(*) FROM outbox WHERE event_id = $1"
    assert await db.fetchval(rows, UUID(task["id"])) == 1


async def test_rejected_write_leaves_no_task_and_no_event(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    tenant, _ = await create_tenant(api)
    tenant_id = UUID(tenant["id"])
    count_tasks = "SELECT count(*) FROM tasks WHERE tenant_id = $1"
    count_events = "SELECT count(*) FROM outbox WHERE payload ->> 'tenant_id' = $1"
    tasks_before = await db.fetchval(count_tasks, tenant_id)
    events_before = await db.fetchval(count_events, tenant["id"])

    stale = await api.patch(f"/v1/tenants/{tenant['id']}", json={"name": "X", "version": 99})

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "tenant_version_conflict"
    assert await db.fetchval(count_tasks, tenant_id) == tasks_before
    assert await db.fetchval(count_events, tenant["id"]) == events_before


async def test_same_update_twice_changes_the_tenant_once(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    tenant, task = await create_tenant(api)
    message_id = uuid4()

    await publish_update(task, "failed", message_id=message_id, error="disk full")
    await publish_update(task, "failed", message_id=message_id, error="disk full")

    await wait_for_status(api, tenant["id"], "failed")
    # Ours (once, however often delivered) plus the worker's in_progress and outcome,
    # both of which now arrive for a finished task and are recorded as stale.
    await eventually(
        lambda: db.fetchval(PROCESSED_FOR_TASK, UUID(task["id"])),
        lambda count: count == 3,
        what="all updates for the task to be processed",
    )
    tenant_now = (await api.get(f"/v1/tenants/{tenant['id']}")).json()
    assert (tenant_now["status"], tenant_now["version"]) == ("failed", 2)


async def test_in_progress_after_done_does_not_regress_the_task(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    tenant, task = await create_tenant(api)
    await publish_update(task, "done")
    await wait_for_status(api, tenant["id"], "active")

    late = await publish_update(task, "in_progress")

    await eventually(
        lambda: db.fetchval("SELECT count(*) FROM processed_messages WHERE message_id = $1", late),
        lambda count: count == 1,
        what="the late in_progress to be processed",
    )
    task_now = (await api.get(f"/v1/tasks/{task['id']}")).json()
    tenant_now = (await api.get(f"/v1/tenants/{tenant['id']}")).json()
    assert task_now["status"] == "done"
    assert (tenant_now["status"], tenant_now["version"]) == ("active", 2)


async def test_poison_messages_are_dead_lettered_and_the_consumer_keeps_going(
    api: httpx2.AsyncClient,
) -> None:
    before = await dead_letter_count()
    tenant, task = await create_tenant(api)
    unknown_task = {"id": str(uuid4()), "tenant_id": str(uuid4())}

    await publish_update(task, "done", body=b"this is not json")
    await publish_update(unknown_task, "done")
    await publish_update(task, "failed", error="disk full")  # a valid update after them

    await wait_for_status(api, tenant["id"], "failed")
    await eventually(
        dead_letter_count,
        lambda count: count == before + 2,
        what="both poison messages in the dead-letter queue",
    )
