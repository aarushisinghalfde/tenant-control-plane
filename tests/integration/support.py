"""Helpers for the integration tests: HTTP calls, polling, and talking to the broker.

The integration tests run against the real stack (API, PostgreSQL, RabbitMQ and the
worker), started by `docker compose --profile tools run --rm --build check`.
"""

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import httpx2

from app.messaging.broker import TASK_UPDATES_DLQ, TASK_UPDATES_EXCHANGE

API_URL = os.environ.get("API_URL", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")

# The worker waits 0.5-2 s per step by default, so a task needs up to ~4 s; leave room
# for a slow laptop.
TIMEOUT_SECONDS = 30.0

Json = dict[str, Any]


def new_slug() -> str:
    """A slug no other test or earlier run has used."""
    return f"it-{uuid4().hex[:16]}"


async def eventually[T](
    probe: Callable[[], Awaitable[T]],
    done: Callable[[T], bool],
    *,
    what: str,
) -> T:
    """Call `probe` until `done(result)` holds; fail with a readable message on timeout."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        result = await probe()
        if done(result):
            return result
        if time.monotonic() > deadline:
            raise AssertionError(
                f"timed out after {TIMEOUT_SECONDS:.0f}s waiting for {what}; last: {result}"
            )
        await asyncio.sleep(0.25)


async def create_tenant(api: httpx2.AsyncClient, slug: str | None = None) -> tuple[Json, Json]:
    response = await api.post("/v1/tenants", json={"slug": slug or new_slug(), "name": "Test"})
    assert response.status_code == 202, response.text
    body: Json = response.json()
    return body["tenant"], body["task"]


async def get_tenant(api: httpx2.AsyncClient, tenant_id: str) -> Json:
    response = await api.get(f"/v1/tenants/{tenant_id}")
    assert response.status_code == 200, response.text
    tenant: Json = response.json()
    return tenant


async def wait_for_status(api: httpx2.AsyncClient, tenant_id: str, status: str) -> Json:
    return await eventually(
        lambda: get_tenant(api, tenant_id),
        lambda tenant: tenant["status"] == status,
        what=f"tenant {tenant_id} to become {status}",
    )


async def active_tenant(api: httpx2.AsyncClient) -> Json:
    """A freshly created tenant, once the worker has deployed it."""
    tenant, _ = await create_tenant(api)
    return await wait_for_status(api, tenant["id"], "active")


async def publish_update(
    task: Json,
    status: str,
    *,
    message_id: UUID | None = None,
    error: str | None = None,
    body: bytes | None = None,
) -> UUID:
    """Publish a hand-made worker update straight to the broker, as the brief's
    "publish messages directly to the broker" scenarios do. Returns its message_id."""
    message_id = message_id or uuid4()
    if body is None:
        update = {
            "schema_version": 1,
            "message_id": str(message_id),
            "task_id": task["id"],
            "tenant_id": task["tenant_id"],
            "status": status,
            "error": error,
            "occurred_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        body = json.dumps(update).encode()
    connection = await aio_pika.connect(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.get_exchange(TASK_UPDATES_EXCHANGE)
        await exchange.publish(
            aio_pika.Message(
                body,
                message_id=str(message_id),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=f"task.update.{status}",
        )
    return message_id


async def dead_letter_count() -> int:
    """Messages currently waiting in the dead-letter queue."""
    connection = await aio_pika.connect(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(TASK_UPDATES_DLQ, passive=True)
        count = queue.declaration_result.message_count
        return int(count or 0)
