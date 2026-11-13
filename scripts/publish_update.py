"""Publish hand-made task updates straight to RabbitMQ, to try the consumer's edge cases.

    docker compose --profile tools run --rm publish <scenario> [task-id]

Runs in a container on the compose network, so it works the same on every OS. Stop the
worker first (`docker compose stop worker`) so only these messages touch the task.
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from uuid import UUID, uuid4

import aio_pika
import httpx2

from app.core.clock import format_timestamp, utc_now
from app.messaging.broker import TASK_UPDATES_EXCHANGE

# scenario -> (what it sends, what the control plane should do)
SCENARIOS: dict[str, tuple[str, str]] = {
    "in_progress": ("one valid in_progress update", "applied (stale if the task already moved on)"),
    "done": ("one valid done update", "applied: task done, tenant moves on (stale if finished)"),
    "failed": ("one valid failed update", "applied: task and tenant failed (stale if finished)"),
    "duplicate": (
        "the same done update twice, same message_id",
        "first applied (or stale), second duplicate: the tenant changes once",
    ),
    "stale": (
        "done, then a late in_progress",
        "done applied (or stale), in_progress stale: the task stays done",
    ),
    "poison": (
        "a body that is not a task update",
        "'poison message ... not a valid task update'; dead-letter queue +1",
    ),
    "unknown-task": (
        "a well-formed update for a task that does not exist",
        "'poison message ... unknown task'; dead-letter queue +1",
    ),
}
NEEDS_TASK = {"in_progress", "done", "failed", "duplicate", "stale"}


@dataclass(frozen=True, slots=True)
class Outgoing:
    routing_key: str
    message_id: UUID
    body: bytes


def update_body(task_id: UUID, tenant_id: UUID, status: str, message_id: UUID) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "message_id": str(message_id),
            "task_id": str(task_id),
            "tenant_id": str(tenant_id),
            "status": status,
            "error": "simulated failure, sent by hand" if status == "failed" else None,
            "occurred_at": format_timestamp(utc_now()),
        }
    ).encode()


def build_messages(scenario: str, task_id: UUID | None, tenant_id: UUID | None) -> list[Outgoing]:
    """The messages a scenario sends, in order. Pure, so it is unit-tested."""

    def one(status: str, message_id: UUID | None = None) -> Outgoing:
        if task_id is None or tenant_id is None:
            raise ValueError(f"scenario {scenario!r} needs a task and its tenant")
        mid = message_id or uuid4()
        return Outgoing(f"task.update.{status}", mid, update_body(task_id, tenant_id, status, mid))

    if scenario in ("in_progress", "done", "failed"):
        return [one(scenario)]
    if scenario == "duplicate":
        first = one("done")
        return [first, first]
    if scenario == "stale":
        return [one("done"), one("in_progress")]
    if scenario == "poison":
        return [Outgoing("task.update.done", uuid4(), b"this is not a task update")]
    if scenario == "unknown-task":
        task_id, tenant_id = uuid4(), uuid4()
        return [one("done")]
    raise ValueError(f"unknown scenario {scenario!r}")


async def tenant_of(task_id: UUID) -> UUID:
    """Look the task up through the API, so the update names the right tenant."""
    async with httpx2.AsyncClient(base_url=os.environ["API_URL"], timeout=10.0) as client:
        response = await client.get(f"/v1/tasks/{task_id}")
    if response.status_code == 404:
        raise SystemExit(f"no task {task_id}; take a task id from GET /v1/tasks")
    response.raise_for_status()
    return UUID(response.json()["tenant_id"])


async def publish(messages: list[Outgoing]) -> None:
    connection = await aio_pika.connect(os.environ["RABBITMQ_URL"])
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.get_exchange(TASK_UPDATES_EXCHANGE)
        for message in messages:
            await exchange.publish(
                aio_pika.Message(
                    message.body,
                    message_id=str(message.message_id),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=message.routing_key,
            )
            print(f"published {message.routing_key:<24} message_id={message.message_id}")


async def run(scenario: str, task_id: UUID | None) -> None:
    tenant_id = await tenant_of(task_id) if task_id is not None else None
    await publish(build_messages(scenario, task_id, tenant_id))
    sends, expect = SCENARIOS[scenario]
    print(f"\nsent: {sends}\nexpect: {expect}")
    print("watch: docker compose logs --tail 20 control-plane")


def main(argv: list[str] | None = None) -> None:
    epilog = "scenarios:\n" + "\n".join(
        f"  {name:<13} {sends}" for name, (sends, _) in SCENARIOS.items()
    )
    parser = argparse.ArgumentParser(
        prog="docker compose --profile tools run --rm publish",
        description="Publish hand-made task updates straight to RabbitMQ.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("scenario", choices=list(SCENARIOS))
    parser.add_argument("task_id", nargs="?", type=UUID, help="a task id from the API")
    args = parser.parse_args(argv)
    if args.scenario in NEEDS_TASK and args.task_id is None:
        parser.error(f"scenario {args.scenario!r} needs a task id")
    asyncio.run(run(args.scenario, args.task_id if args.scenario in NEEDS_TASK else None))


if __name__ == "__main__":
    main()
