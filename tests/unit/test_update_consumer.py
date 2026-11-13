"""Unit tests for how the update consumer settles each delivery."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.models import TaskStatus
from app.messaging.consumer import UpdateConsumer, delivery_attempt
from app.messaging.events import TaskUpdate
from app.services.task_update_service import UnusableUpdateError, UpdateOutcome


def update_body() -> bytes:
    update = TaskUpdate(
        message_id=uuid4(),
        task_id=uuid4(),
        tenant_id=uuid4(),
        status=TaskStatus.DONE,
        occurred_at=datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC),
    )
    return update.model_dump_json().encode()


class FakeMessage:
    def __init__(self, body: bytes, delivery_count: int | None = None) -> None:
        self.body = body
        self.message_id = "m-1"
        self.headers = {} if delivery_count is None else {"x-delivery-count": delivery_count}
        self.outcome: str | None = None

    async def ack(self) -> None:
        self.outcome = "ack"

    async def nack(self, requeue: bool = True) -> None:
        self.outcome = f"nack(requeue={requeue})"

    async def reject(self, requeue: bool = False) -> None:
        self.outcome = f"reject(requeue={requeue})"


class FakeService:
    def __init__(self, result: UpdateOutcome | Exception) -> None:
        self.result = result
        self.calls = 0

    async def apply(self, update: TaskUpdate) -> UpdateOutcome:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def consumer(service: FakeService) -> UpdateConsumer:
    return UpdateConsumer(service, max_attempts=3, retry_delay=lambda attempt: 0.0)


async def handle(c: UpdateConsumer, message: FakeMessage) -> None:
    await c.handle(message)  # type: ignore[arg-type]


@pytest.mark.parametrize("outcome", list(UpdateOutcome))
async def test_every_outcome_is_acked(outcome: UpdateOutcome) -> None:
    message = FakeMessage(update_body())

    await handle(consumer(FakeService(outcome)), message)

    assert message.outcome == "ack"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"{}",
        b'{"message_id": "x", "status": "done"}',
        b'{"status": "accepted"}',
    ],
)
async def test_malformed_message_is_dead_lettered_without_calling_the_service(
    body: bytes,
) -> None:
    service = FakeService(UpdateOutcome.APPLIED)
    message = FakeMessage(body)

    await handle(consumer(service), message)

    assert message.outcome == "reject(requeue=False)"
    assert service.calls == 0


async def test_unusable_update_is_dead_lettered() -> None:
    message = FakeMessage(update_body())

    await handle(consumer(FakeService(UnusableUpdateError("unknown task"))), message)

    assert message.outcome == "reject(requeue=False)"


async def test_temporary_failure_is_requeued() -> None:
    message = FakeMessage(update_body())

    await handle(consumer(FakeService(ConnectionError("database down"))), message)

    assert message.outcome == "nack(requeue=True)"


async def test_gives_up_after_max_attempts() -> None:
    message = FakeMessage(update_body(), delivery_count=2)  # third delivery, max is 3

    await handle(consumer(FakeService(ConnectionError("database down"))), message)

    assert message.outcome == "reject(requeue=False)"


async def test_next_message_is_still_processed_after_a_poison_one() -> None:
    c = consumer(FakeService(UpdateOutcome.APPLIED))
    poison, good = FakeMessage(b"garbage"), FakeMessage(update_body())

    await handle(c, poison)
    await handle(c, good)

    assert (poison.outcome, good.outcome) == ("reject(requeue=False)", "ack")


@pytest.mark.parametrize(
    ("headers", "attempt"),
    [
        ({}, 1),
        ({"x-delivery-count": 0}, 1),
        ({"x-delivery-count": 4}, 5),
        ({"x-delivery-count": "x"}, 1),
    ],
)
def test_delivery_attempt(headers: dict[str, object], attempt: int) -> None:
    message = FakeMessage(b"")
    message.headers = headers  # type: ignore[assignment]

    assert delivery_attempt(message) == attempt  # type: ignore[arg-type]
