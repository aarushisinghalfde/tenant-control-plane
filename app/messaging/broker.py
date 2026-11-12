"""RabbitMQ connection, topology and publishing.

Topology, declared idempotently by every process that connects (declaring an
existing exchange or queue with the same settings is a no-op):

    tasks (topic) --"task.*"--> worker.tasks                       control plane -> worker
    task-updates (topic) --"task.update.*"--> controlplane.task-updates   worker -> control plane
        rejected updates --> task-updates.dlx (fanout) --> controlplane.task-updates.dlq

All queues are durable quorum queues and every message is persistent, so nothing
queued is lost when the broker restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import aio_pika
from aio_pika import DeliveryMode, ExchangeType
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

TASKS_EXCHANGE: Final = "tasks"
WORKER_TASKS_QUEUE: Final = "worker.tasks"
TASK_UPDATES_EXCHANGE: Final = "task-updates"
TASK_UPDATES_QUEUE: Final = "controlplane.task-updates"
TASK_UPDATES_DLX: Final = "task-updates.dlx"
TASK_UPDATES_DLQ: Final = "controlplane.task-updates.dlq"

PUBLISH_TIMEOUT_SECONDS: Final = 5.0

# What a failed publish can raise: a broker error (a nack or an unroutable message
# returned to us), a lost connection, a channel used while it reconnects
# (aio-pika raises a RuntimeError subclass), or no confirm within the timeout.
PUBLISH_ERRORS: Final = (aio_pika.exceptions.AMQPError, ConnectionError, RuntimeError, TimeoutError)


@dataclass(frozen=True, slots=True)
class Exchanges:
    tasks: AbstractExchange
    task_updates: AbstractExchange


async def declare_topology(channel: AbstractChannel) -> Exchanges:
    """Create (or confirm) every exchange, queue and binding both processes rely on."""
    tasks = await channel.declare_exchange(TASKS_EXCHANGE, ExchangeType.TOPIC, durable=True)
    worker_tasks = await channel.declare_queue(
        WORKER_TASKS_QUEUE, durable=True, arguments={"x-queue-type": "quorum"}
    )
    await worker_tasks.bind(tasks, routing_key="task.*")

    task_updates = await channel.declare_exchange(
        TASK_UPDATES_EXCHANGE, ExchangeType.TOPIC, durable=True
    )
    dead_letters = await channel.declare_exchange(
        TASK_UPDATES_DLX, ExchangeType.FANOUT, durable=True
    )
    dlq = await channel.declare_queue(
        TASK_UPDATES_DLQ, durable=True, arguments={"x-queue-type": "quorum"}
    )
    await dlq.bind(dead_letters)
    updates = await channel.declare_queue(
        TASK_UPDATES_QUEUE,
        durable=True,
        # A message the consumer rejects (poison) is re-routed here instead of dropped.
        arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": TASK_UPDATES_DLX},
    )
    await updates.bind(task_updates, routing_key="task.update.*")

    return Exchanges(tasks=tasks, task_updates=task_updates)


class Broker:
    """The control plane's link to RabbitMQ: one self-healing connection, one channel."""

    def __init__(self, connection: AbstractRobustConnection, exchanges: Exchanges) -> None:
        self._connection = connection
        self._exchanges = exchanges

    @classmethod
    async def connect(cls, url: str) -> Broker:
        """Connect and declare the topology.

        `connect_robust` reconnects (and re-declares) by itself after a network or broker
        failure. Publisher confirms make `publish` wait until the broker has taken
        responsibility for the message; `on_return_raises` turns an unroutable message
        into an error instead of a silent drop.
        """
        connection = await aio_pika.connect_robust(url)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchanges = await declare_topology(channel)
        except BaseException:
            await connection.close()
            raise
        return cls(connection, exchanges)

    @property
    def is_connected(self) -> bool:
        return not self._connection.is_closed and self._connection.connected.is_set()

    async def publish_task(self, *, message_id: UUID, routing_key: str, body: bytes) -> None:
        """Publish one task event; returns only once the broker has confirmed it.

        Raises one of PUBLISH_ERRORS if the broker is unreachable or refuses the message.
        """
        message = aio_pika.Message(
            body,
            message_id=str(message_id),  # = task id; lets consumers drop duplicates
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
        )
        await self._exchanges.tasks.publish(
            message, routing_key=routing_key, timeout=PUBLISH_TIMEOUT_SECONDS
        )

    async def close(self) -> None:
        await self._connection.close()
