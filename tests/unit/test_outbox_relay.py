"""Unit tests for the outbox relay, with an in-memory outbox and a fake broker.

The real SQL (FOR UPDATE SKIP LOCKED) and real RabbitMQ are covered by the
integration tests; these pin down the relay's own decisions.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from app.db.database import Database
from app.db.repository import OutboxEvent
from app.messaging.outbox_relay import BatchResult, OutboxRelay

NOW = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)


class FakeOutbox:
    """Plays both the Database and the Repository the relay talks to."""

    def __init__(self, count: int) -> None:
        self.events = [
            OutboxEvent(
                id=n,
                event_id=uuid4(),
                routing_key="task.deploy",
                payload=f'{{"n": {n}}}',
                attempts=0,
            )
            for n in range(1, count + 1)
        ]
        self.published: dict[int, datetime] = {}
        self.failures: dict[int, int] = {}
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["FakeOutbox"]:
        self.transactions += 1
        yield self

    async def claim_outbox_batch(self, limit: int) -> list[OutboxEvent]:
        return [e for e in self.events if e.id not in self.published][:limit]

    async def mark_outbox_published(self, outbox_id: int, now: datetime) -> None:
        self.published[outbox_id] = now

    async def record_outbox_failure(self, outbox_id: int) -> None:
        self.failures[outbox_id] = self.failures.get(outbox_id, 0) + 1


class FakeBroker:
    def __init__(self, fail_on: set[UUID] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.sent: list[tuple[UUID, str, bytes]] = []

    async def publish_task(self, *, message_id: UUID, routing_key: str, body: bytes) -> None:
        if message_id in self.fail_on:
            raise ConnectionError("broker unreachable")
        self.sent.append((message_id, routing_key, body))


def relay(outbox: FakeOutbox, broker: FakeBroker, batch_size: int = 100) -> OutboxRelay:
    return OutboxRelay(cast(Database, outbox), broker, batch_size=batch_size, clock=lambda: NOW)


async def test_publishes_pending_events_in_order_and_marks_them() -> None:
    outbox, broker = FakeOutbox(3), FakeBroker()

    result = await relay(outbox, broker).publish_batch()

    assert result == BatchResult(published=3, failed=False)
    assert [message_id for message_id, _, _ in broker.sent] == [e.event_id for e in outbox.events]
    assert broker.sent[0][1:] == ("task.deploy", b'{"n": 1}')
    assert outbox.published == {1: NOW, 2: NOW, 3: NOW}


async def test_message_id_is_the_event_id() -> None:
    outbox, broker = FakeOutbox(1), FakeBroker()

    await relay(outbox, broker).publish_batch()

    assert broker.sent[0][0] == outbox.events[0].event_id


async def test_failure_stops_the_batch_and_counts_an_attempt() -> None:
    outbox = FakeOutbox(3)
    broker = FakeBroker(fail_on={outbox.events[1].event_id})

    result = await relay(outbox, broker).publish_batch()

    assert result == BatchResult(published=1, failed=True)
    assert set(outbox.published) == {1}  # sent before the failure: stays marked
    assert outbox.failures == {2: 1}  # the failing row is retried later
    assert len(broker.sent) == 1  # row 3 was not attempted, keeping the order


async def test_failed_event_is_published_once_the_broker_recovers() -> None:
    outbox = FakeOutbox(2)
    broker = FakeBroker(fail_on={outbox.events[0].event_id})
    outbox_relay = relay(outbox, broker)

    await outbox_relay.publish_batch()
    broker.fail_on.clear()
    result = await outbox_relay.publish_batch()

    assert result == BatchResult(published=2, failed=False)
    assert set(outbox.published) == {1, 2}


async def test_batch_size_limits_one_pass() -> None:
    outbox, broker = FakeOutbox(5), FakeBroker()

    result = await relay(outbox, broker, batch_size=2).publish_batch()

    assert result.published == 2
    assert set(outbox.published) == {1, 2}


async def test_nothing_pending_publishes_nothing() -> None:
    outbox, broker = FakeOutbox(0), FakeBroker()

    result = await relay(outbox, broker).publish_batch()

    assert result == BatchResult(published=0, failed=False)
    assert broker.sent == []
