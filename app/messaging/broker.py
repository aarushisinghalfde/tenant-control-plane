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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import aio_pika
from aio_pika import DeliveryMode, ExchangeType
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)

logger = logging.getLogger(__name__)

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


MessageHandler = Callable[[AbstractIncomingMessage], Awaitable[None]]


class Broker:
    """One process's link to RabbitMQ: a self-healing connection and a publishing channel.

    The control plane uses it to publish tasks (and, from phase 7, to consume updates);
    the worker uses it to consume tasks and publish updates.
    """

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
        """Publish one task event to the worker; returns once the broker has confirmed it.

        Raises one of PUBLISH_ERRORS if the broker is unreachable or refuses the message.
        """
        await _publish(self._exchanges.tasks, message_id, routing_key, body)

    async def publish_update(self, *, message_id: UUID, routing_key: str, body: bytes) -> None:
        """Publish one task update to the control plane; same guarantees as publish_task."""
        await _publish(self._exchanges.task_updates, message_id, routing_key, body)

    async def consume(self, queue_name: str, handler: MessageHandler, *, prefetch: int) -> None:
        """Start delivering messages from `queue_name` to `handler`, then return.

        The broker hands out at most `prefetch` unacknowledged messages at a time, each in
        its own asyncio task. The handler must ack, nack or reject every message itself.
        Consumption resumes on its own after a reconnect.
        """
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=prefetch)
        queue = await channel.get_queue(queue_name)
        await queue.consume(handler)

    async def close(self) -> None:
        await self._connection.close()


async def _publish(
    exchange: AbstractExchange, message_id: UUID, routing_key: str, body: bytes
) -> None:
    message = aio_pika.Message(
        body,
        message_id=str(message_id),  # lets consumers drop duplicates
        content_type="application/json",
        delivery_mode=DeliveryMode.PERSISTENT,
    )
    await exchange.publish(message, routing_key=routing_key, timeout=PUBLISH_TIMEOUT_SECONDS)


async def settle(action: Awaitable[None]) -> None:
    """Await an ack, nack or reject without raising.

    If the channel is already gone the broker never got the answer and will redeliver
    the message, which every consumer here handles; so log and carry on.
    """
    try:
        await action
    except Exception:
        logger.warning("could not ack/nack/reject; the broker will redeliver", exc_info=True)
