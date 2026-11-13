"""Worker simulator: consumes provisioning tasks and reports progress over the broker.

    python -m worker.main --fail-rate 0.2 --min-delay-ms 200 --max-delay-ms 1000

For each task on `worker.tasks` it waits, publishes `in_progress`, waits, publishes
`done` or `failed`, and only then acknowledges the task. If it dies half-way, the
broker redelivers the task and it is simulated again; the update ids are derived
from the task id, so the control plane sees the repeats as duplicates.
"""

import asyncio
import logging
import os
import random
import signal
import sys
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError

from app.core.clock import utc_now
from app.domain.models import TaskStatus
from app.messaging.broker import WORKER_TASKS_QUEUE, Broker, settle
from app.messaging.events import TaskEvent, TaskUpdate
from worker.config import WorkerConfig, parse_config
from worker.simulation import FAILURE_REASON, plan_steps

logger = logging.getLogger("worker")


class UpdatePublisher(Protocol):
    """What the worker needs from the broker; `Broker` satisfies it, tests use a fake."""

    async def publish_update(self, *, message_id: UUID, routing_key: str, body: bytes) -> None: ...


class Worker:
    def __init__(
        self,
        broker: UpdatePublisher,
        config: WorkerConfig,
        rng: random.Random,
        *,
        requeue_pause: float = 1.0,
    ) -> None:
        self._broker = broker
        self._config = config
        self._rng = rng
        self._requeue_pause = requeue_pause

    async def handle(self, message: AbstractIncomingMessage) -> None:
        """Process one delivery. Never raises: an error here would go unnoticed."""
        try:
            event = TaskEvent.model_validate_json(message.body)
        except ValidationError:
            # Not a task we understand; retrying cannot fix it, so drop it loudly.
            logger.error("rejecting malformed task message %s", message.message_id)
            await message.reject(requeue=False)
            return

        try:
            await self._simulate(event)
        except Exception:
            # Typically the broker went away mid-task. Put the task back so it is
            # done again later; the pause keeps a persistent fault from spinning.
            logger.exception("task %s: could not report progress; requeueing", event.id)
            await asyncio.sleep(self._requeue_pause)
            await settle(message.nack(requeue=True))
            return

        await settle(message.ack())  # only after every update was confirmed

    async def _simulate(self, event: TaskEvent) -> None:
        logger.info("task %s: %s for tenant %s", event.id, event.type, event.tenant.slug)
        last_body = b""
        last_update: TaskUpdate | None = None
        for step in plan_steps(self._config, self._rng):
            await asyncio.sleep(step.delay)
            if not step.duplicate or last_update is None:
                last_update = TaskUpdate.for_task(
                    event,
                    step.status,
                    occurred_at=utc_now(),
                    error=FAILURE_REASON if step.status is TaskStatus.FAILED else None,
                )
                last_body = last_update.model_dump_json().encode()
            await self._broker.publish_update(
                message_id=last_update.message_id,
                routing_key=last_update.routing_key,
                body=last_body,
            )
            note = " (duplicate)" if step.duplicate else ""
            logger.info("task %s: sent %s%s", event.id, step.status, note)


async def run(config: WorkerConfig) -> None:
    broker = await Broker.connect(config.rabbitmq_url)
    # Not for security: the simulator only decides delays and outcomes.
    rng = random.Random(config.seed)  # nosec B311
    worker = Worker(broker, config, rng)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await broker.consume(WORKER_TASKS_QUEUE, worker.handle, prefetch=config.concurrency)
        logger.info(
            "worker ready: fail_rate=%s delay=%s-%sms duplicate_rate=%s reorder=%s "
            "seed=%s concurrency=%s",
            config.fail_rate,
            config.min_delay_ms,
            config.max_delay_ms,
            config.duplicate_rate,
            config.reorder,
            config.seed,
            config.concurrency,
        )
        await stop.wait()
        logger.info("stopping; unfinished tasks will be redelivered")
    finally:
        await broker.close()


def main(argv: Sequence[str] | None = None) -> None:
    config = parse_config(sys.argv[1:] if argv is None else argv, os.environ)
    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
