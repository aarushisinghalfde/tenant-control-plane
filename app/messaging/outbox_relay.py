"""Transactional outbox relay: publishes committed events to the broker.

Request handlers never talk to the broker. They INSERT an outbox row in the same
transaction as the tenant and task, so the row exists if and only if the change
committed. This background loop then publishes committed, unpublished rows and
marks them. If the process dies between commit and publish, the row is still
there and goes out after the restart, so no event is ever lost.

Delivery is at-least-once: a crash after the broker confirms but before the row is
marked sends that event again. Consumers drop duplicates by message id (= task id).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.core.backoff import backoff_delay
from app.core.clock import Clock, utc_now
from app.db.database import Database
from app.messaging.broker import PUBLISH_ERRORS

logger = logging.getLogger(__name__)


class TaskPublisher(Protocol):
    """What the relay needs from the broker; `Broker` satisfies it, tests use a fake."""

    async def publish_task(self, *, message_id: UUID, routing_key: str, body: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class BatchResult:
    published: int
    failed: bool


class OutboxRelay:
    def __init__(
        self,
        db: Database,
        publisher: TaskPublisher,
        *,
        batch_size: int = 100,
        poll_interval: float = 0.2,
        clock: Clock = utc_now,
    ) -> None:
        self._db = db
        self._publisher = publisher
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._clock = clock

    async def run(self) -> None:
        """Publish until cancelled. Started at app startup, cancelled at shutdown."""
        failures = 0
        while True:
            try:
                result = await self.publish_batch()
            except Exception:  # e.g. the database is down; the loop must survive it
                logger.exception("outbox relay iteration failed")
                result = BatchResult(published=0, failed=True)

            if result.failed:
                failures += 1
                await asyncio.sleep(backoff_delay(failures))
            elif result.published == self._batch_size:
                failures = 0  # a full batch: more rows are probably waiting, go again now
            else:
                failures = 0
                await asyncio.sleep(self._poll_interval)

    async def publish_batch(self) -> BatchResult:
        """Publish up to one batch of pending events, oldest first.

        Stops at the first failure: that row's `attempts` goes up, the rows before it
        stay marked as published (the transaction still commits), and the rest wait
        for the next try.
        """
        published = 0
        async with self._db.transaction() as repo:
            for event in await repo.claim_outbox_batch(self._batch_size):
                try:
                    await self._publisher.publish_task(
                        message_id=event.event_id,
                        routing_key=event.routing_key,
                        body=event.payload.encode(),
                    )
                except PUBLISH_ERRORS:
                    logger.warning(
                        "publishing event %s failed (attempt %d); will retry",
                        event.event_id,
                        event.attempts + 1,
                        exc_info=True,
                    )
                    await repo.record_outbox_failure(event.id)
                    return BatchResult(published=published, failed=True)
                await repo.mark_outbox_published(event.id, self._clock())
                published += 1
        return BatchResult(published=published, failed=False)
