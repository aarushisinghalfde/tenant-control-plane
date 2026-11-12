"""Connection pool, schema migrations and the unit of work (one transaction, one repository)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from app.db.repository import Repository

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
# Arbitrary constant: an advisory lock key so concurrent replicas migrate one at a time.
_MIGRATION_LOCK_KEY = 7_406_243


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=10)


def _load_migrations() -> list[tuple[str, str]]:
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [(path.name, path.read_text(encoding="utf-8")) for path in paths]


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply every migration file not yet recorded in schema_migrations, in name order."""
    migrations = _load_migrations()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_KEY)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     text        PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in rows}
        for name, sql in migrations:
            if name in applied:
                continue
            logger.info("applying migration %s", name)
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", name)


class Database:
    """Hands out repositories bound to a pooled connection."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Repository]:
        """All-or-nothing: commits when the block exits normally, rolls back on any exception."""
        async with self._pool.acquire() as conn, conn.transaction():
            yield Repository(conn)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Repository]:
        async with self._pool.acquire() as conn:
            yield Repository(conn)

    async def ping(self) -> bool:
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            return False
        return True
