"""All SQL for tenants, tasks and the outbox.

Every statement is written out in full so the exact query is easy to review. The
concurrency guarantees live here: unique constraints and conditional UPDATEs.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.domain.errors import TenantAlreadyExistsError
from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """An outbox row waiting to be published. `payload` is the JSON message body."""

    id: int
    event_id: UUID
    routing_key: str
    payload: str
    attempts: int


class Repository:
    """All SQL for tenants, tasks and the outbox, bound to one connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    # --- tenants ----------------------------------------------------------------

    async def insert_tenant(self, tenant: Tenant) -> None:
        try:
            await self._conn.execute(
                """
                INSERT INTO tenants (id, slug, name, status, version, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                tenant.id,
                tenant.slug,
                tenant.name,
                tenant.status.value,
                tenant.version,
                tenant.created_at,
                tenant.updated_at,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "tenants_slug_unique":
                raise TenantAlreadyExistsError(f"slug {tenant.slug!r} is already taken") from exc
            raise

    async def get_tenant(self, tenant_id: UUID) -> Tenant | None:
        row = await self._conn.fetchrow(
            """
            SELECT id, slug, name, status, version, created_at, updated_at
            FROM tenants
            WHERE id = $1
            """,
            tenant_id,
        )
        return _tenant(row) if row is not None else None

    async def list_tenants(
        self,
        *,
        status: TenantStatus | None,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[Tenant]:
        after_created_at, after_id = after if after else (None, None)
        rows = await self._conn.fetch(
            """
            SELECT id, slug, name, status, version, created_at, updated_at
            FROM tenants
            WHERE ($1::text IS NULL OR status = $1::text)
              AND ($2::timestamptz IS NULL OR (created_at, id) < ($2::timestamptz, $3::uuid))
            ORDER BY created_at DESC, id DESC
            LIMIT $4
            """,
            status.value if status else None,
            after_created_at,
            after_id,
            limit,
        )
        return [_tenant(row) for row in rows]

    async def transition_tenant(
        self,
        tenant_id: UUID,
        *,
        new_status: TenantStatus,
        allowed_from: Collection[TenantStatus],
        now: datetime,
        expected_version: int | None = None,
        name: str | None = None,
    ) -> Tenant | None:
        """Move a tenant to `new_status` only if every guard holds, in ONE statement.

        Guards: the current status is in `allowed_from` and, when given, the version
        matches `expected_version` (optimistic lock). Postgres locks the row, so of two
        concurrent calls with the same version exactly one matches; the other sees the
        bumped version and gets None. None means "rejected": the caller works out why.
        """
        row = await self._conn.fetchrow(
            """
            UPDATE tenants
            SET status = $2,
                name = COALESCE($5, name),
                version = version + 1,
                updated_at = $6
            WHERE id = $1
              AND status = ANY($3::text[])
              AND ($4::integer IS NULL OR version = $4::integer)
            RETURNING id, slug, name, status, version, created_at, updated_at
            """,
            tenant_id,
            new_status.value,
            [status.value for status in allowed_from],
            expected_version,
            name,
            now,
        )
        return _tenant(row) if row is not None else None

    # --- tasks ------------------------------------------------------------------

    async def insert_task(self, task: Task) -> None:
        await self._conn.execute(
            """
            INSERT INTO tasks (id, tenant_id, type, status, error, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            task.id,
            task.tenant_id,
            task.type.value,
            task.status.value,
            task.error,
            task.created_at,
            task.updated_at,
        )

    async def get_task(self, task_id: UUID) -> Task | None:
        row = await self._conn.fetchrow(
            """
            SELECT id, tenant_id, type, status, error, created_at, updated_at
            FROM tasks
            WHERE id = $1
            """,
            task_id,
        )
        return _task(row) if row is not None else None

    async def lock_task(self, task_id: UUID) -> Task | None:
        """Read a task and lock its row until the transaction ends.

        Two updates for the same task (say `in_progress` and `done` delivered in parallel)
        then run one after the other, each seeing the other's result.
        """
        row = await self._conn.fetchrow(
            """
            SELECT id, tenant_id, type, status, error, created_at, updated_at
            FROM tasks
            WHERE id = $1
            FOR UPDATE
            """,
            task_id,
        )
        return _task(row) if row is not None else None

    async def update_task_status(
        self, task_id: UUID, *, status: TaskStatus, error: str | None, now: datetime
    ) -> None:
        await self._conn.execute(
            """
            UPDATE tasks
            SET status = $2, error = $3, updated_at = $4
            WHERE id = $1
            """,
            task_id,
            status.value,
            error,
            now,
        )

    async def list_tasks(
        self,
        *,
        tenant_id: UUID | None,
        task_type: TaskType | None,
        status: TaskStatus | None,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[Task]:
        after_created_at, after_id = after if after else (None, None)
        rows = await self._conn.fetch(
            """
            SELECT id, tenant_id, type, status, error, created_at, updated_at
            FROM tasks
            WHERE ($1::uuid IS NULL OR tenant_id = $1::uuid)
              AND ($2::text IS NULL OR type = $2::text)
              AND ($3::text IS NULL OR status = $3::text)
              AND ($4::timestamptz IS NULL OR (created_at, id) < ($4::timestamptz, $5::uuid))
            ORDER BY created_at DESC, id DESC
            LIMIT $6
            """,
            tenant_id,
            task_type.value if task_type else None,
            status.value if status else None,
            after_created_at,
            after_id,
            limit,
        )
        return [_task(row) for row in rows]

    # --- inbox (consumer idempotency) ----------------------------------------------

    async def record_processed_message(self, message_id: UUID, task_id: UUID) -> bool:
        """Remember that an update was applied; False if it already was (a duplicate).

        Runs in the same transaction as the change it guards, so "applied" and
        "recorded" can never disagree: both commit or neither does.
        """
        inserted = await self._conn.fetchval(
            """
            INSERT INTO processed_messages (message_id, task_id)
            VALUES ($1, $2)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING message_id
            """,
            message_id,
            task_id,
        )
        return inserted is not None

    # --- outbox -----------------------------------------------------------------

    async def insert_outbox(self, *, event_id: UUID, routing_key: str, payload: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO outbox (event_id, routing_key, payload)
            VALUES ($1, $2, $3::jsonb)
            """,
            event_id,
            routing_key,
            payload,
        )

    async def claim_outbox_batch(self, limit: int) -> list[OutboxEvent]:
        """Lock up to `limit` unpublished events, oldest first.

        FOR UPDATE holds the rows until this transaction ends, so no one else publishes
        them meanwhile; SKIP LOCKED lets a second relay (another replica) take the next
        rows instead of waiting. Only committed rows are visible here, which is what
        makes "publish after commit" hold.
        """
        rows = await self._conn.fetch(
            """
            SELECT id, event_id, routing_key, payload::text AS payload, attempts
            FROM outbox
            WHERE published_at IS NULL
            ORDER BY id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            limit,
        )
        return [
            OutboxEvent(
                id=row["id"],
                event_id=row["event_id"],
                routing_key=row["routing_key"],
                payload=row["payload"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def mark_outbox_published(self, outbox_id: int, now: datetime) -> None:
        await self._conn.execute(
            "UPDATE outbox SET published_at = $2 WHERE id = $1",
            outbox_id,
            now,
        )

    async def record_outbox_failure(self, outbox_id: int) -> None:
        await self._conn.execute(
            "UPDATE outbox SET attempts = attempts + 1 WHERE id = $1",
            outbox_id,
        )


def _tenant(row: Any) -> Tenant:
    return Tenant(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        status=TenantStatus(row["status"]),
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _task(row: Any) -> Task:
    return Task(
        id=row["id"],
        tenant_id=row["tenant_id"],
        type=TaskType(row["type"]),
        status=TaskStatus(row["status"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
