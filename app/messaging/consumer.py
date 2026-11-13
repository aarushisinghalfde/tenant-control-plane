"""Consumer of the worker's progress updates (queue `controlplane.task-updates`).

Per delivery:

1. Parse it. Anything that is not a valid `TaskUpdate` is poison: rejected without
   requeue, so RabbitMQ moves it to the dead-letter queue.
2. Apply it with `TaskUpdateService`, in one transaction (dedupe, forward-only, tenant).
3. Ack only after that transaction commits. A crash before the ack means redelivery,
   which the idempotency record turns into a harmless duplicate.
4. A temporary failure (say the database is down) is retried with backoff by
   requeueing; after `max_attempts` deliveries the message is dead-lettered instead,
   so it cannot block the queue forever.

`handle` never raises: one bad message must not stop the consumer.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError

from app.core.backoff import backoff_delay
from app.messaging.broker import settle
from app.messaging.events import TaskUpdate
from app.services.task_update_service import UnusableUpdateError, UpdateOutcome

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Updates applied at once. Each holds a database connection while it runs, and the
# pool (10) is shared with the API and the outbox relay, so keep this well below it.
PREFETCH = 4


class UpdateApplier(Protocol):
    """What the consumer needs; `TaskUpdateService` satisfies it, tests use a fake."""

    async def apply(self, update: TaskUpdate) -> UpdateOutcome: ...


class UpdateConsumer:
    def __init__(
        self,
        service: UpdateApplier,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        retry_delay: Callable[[int], float] = backoff_delay,
    ) -> None:
        self._service = service
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay

    async def handle(self, message: AbstractIncomingMessage) -> None:
        try:
            update = TaskUpdate.model_validate_json(message.body)
        except ValidationError as exc:
            problems = exc.error_count()
            await self._dead_letter(
                message,
                f"not a valid task update ({problems} problem{'s' if problems != 1 else ''})",
            )
            return

        try:
            outcome = await self._service.apply(update)
        except UnusableUpdateError as exc:
            await self._dead_letter(message, str(exc))
            return
        except Exception:
            await self._retry_or_give_up(message, update)
            return

        logger.info(
            "update %s for task %s (%s): %s",
            update.message_id,
            update.task_id,
            update.status,
            outcome,
        )
        await settle(message.ack())

    async def _retry_or_give_up(self, message: AbstractIncomingMessage, update: TaskUpdate) -> None:
        attempt = delivery_attempt(message)
        if attempt >= self._max_attempts:
            logger.exception(
                "update %s failed on attempt %d of %d; dead-lettering it",
                update.message_id,
                attempt,
                self._max_attempts,
            )
            await settle(message.reject(requeue=False))
            return
        delay = self._retry_delay(attempt)
        logger.warning(
            "update %s failed on attempt %d of %d; retrying in %.1fs",
            update.message_id,
            attempt,
            self._max_attempts,
            delay,
            exc_info=True,
        )
        await asyncio.sleep(delay)
        await settle(message.nack(requeue=True))

    @staticmethod
    async def _dead_letter(message: AbstractIncomingMessage, reason: str) -> None:
        logger.error("poison message %s: %s; dead-lettering it", message.message_id, reason)
        await settle(message.reject(requeue=False))


def delivery_attempt(message: AbstractIncomingMessage) -> int:
    """Which delivery this is, starting at 1.

    Quorum queues count earlier failed deliveries in the `x-delivery-count` header,
    which is absent on the first delivery.
    """
    count = (message.headers or {}).get("x-delivery-count")
    return count + 1 if isinstance(count, int) else 1
