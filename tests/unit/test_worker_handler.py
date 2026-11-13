"""Unit tests for how the worker settles each delivery, with a fake broker and message."""

import json
import random
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

import pytest

from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.messaging.events import TaskEvent
from worker.config import WorkerConfig
from worker.main import Worker

NOW = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
CONFIG = WorkerConfig(
    rabbitmq_url="amqp://unused",
    fail_rate=0.0,
    min_delay_ms=0,
    max_delay_ms=0,
    duplicate_rate=0.0,
    reorder=False,
    seed=1,
    concurrency=1,
    log_level="INFO",
)


def task_event() -> TaskEvent:
    tenant = Tenant(
        id=uuid4(),
        slug="acme",
        name="Acme",
        status=TenantStatus.PROVISIONING,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    task = Task(
        id=uuid4(),
        tenant_id=tenant.id,
        type=TaskType.DEPLOY,
        status=TaskStatus.ACCEPTED,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return TaskEvent.for_task(task, tenant)


class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.message_id = "m-1"
        self.outcome: str | None = None

    async def ack(self) -> None:
        self.outcome = "ack"

    async def nack(self, requeue: bool = True) -> None:
        self.outcome = f"nack(requeue={requeue})"

    async def reject(self, requeue: bool = False) -> None:
        self.outcome = f"reject(requeue={requeue})"


class FakeBroker:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[UUID, str, dict[str, object]]] = []

    async def publish_update(self, *, message_id: UUID, routing_key: str, body: bytes) -> None:
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.sent.append((message_id, routing_key, json.loads(body)))


def worker(broker: FakeBroker, config: WorkerConfig = CONFIG) -> Worker:
    return Worker(broker, config, random.Random(1), requeue_pause=0.0)


async def handle(w: Worker, message: FakeMessage) -> None:
    await w.handle(message)  # type: ignore[arg-type]


async def test_reports_progress_then_acks() -> None:
    event = task_event()
    broker, message = FakeBroker(), FakeMessage(event.model_dump_json().encode())

    await handle(worker(broker), message)

    assert [key for _, key, _ in broker.sent] == ["task.update.in_progress", "task.update.done"]
    first = broker.sent[0][2]
    assert first["task_id"] == str(event.id)
    assert first["tenant_id"] == str(event.tenant_id)
    assert first["error"] is None
    assert message.outcome == "ack"


async def test_redoing_a_task_reuses_the_same_update_ids() -> None:
    """A redelivered task is simulated again; the consumer must see repeats as duplicates."""
    event = task_event()
    broker = FakeBroker()

    await handle(worker(broker), FakeMessage(event.model_dump_json().encode()))
    await handle(worker(broker), FakeMessage(event.model_dump_json().encode()))

    ids = [message_id for message_id, _, _ in broker.sent]
    assert ids[0] == ids[2] == uuid5(event.id, "in_progress")
    assert ids[1] == ids[3] == uuid5(event.id, "done")


async def test_failed_outcome_carries_a_reason() -> None:
    broker = FakeBroker()
    message = FakeMessage(task_event().model_dump_json().encode())

    await handle(worker(broker, replace(CONFIG, fail_rate=1.0)), message)

    _, key, body = broker.sent[-1]
    assert key == "task.update.failed"
    assert body["status"] == "failed"
    assert body["error"]


async def test_duplicate_is_byte_for_byte_the_same_message() -> None:
    broker = FakeBroker()
    message = FakeMessage(task_event().model_dump_json().encode())

    await handle(worker(broker, replace(CONFIG, duplicate_rate=1.0)), message)

    assert len(broker.sent) == 4
    assert broker.sent[0] == broker.sent[1]
    assert broker.sent[2] == broker.sent[3]


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"id": "nope"}'])
async def test_malformed_task_is_rejected_without_requeue(body: bytes) -> None:
    broker, message = FakeBroker(), FakeMessage(body)

    await handle(worker(broker), message)

    assert message.outcome == "reject(requeue=False)"
    assert broker.sent == []


async def test_broker_failure_requeues_the_task() -> None:
    broker = FakeBroker(fail=True)
    message = FakeMessage(task_event().model_dump_json().encode())

    await handle(worker(broker), message)

    assert message.outcome == "nack(requeue=True)"
