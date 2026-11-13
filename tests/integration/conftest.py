"""Fixtures for the integration tests, which need the whole stack running.

`docker compose --profile tools run --rm --build check` starts it and sets API_URL,
DATABASE_URL and RABBITMQ_URL. Without them (say, a bare `pytest` on a laptop) these
tests are skipped, and the summary says why.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx2
import pytest

from tests.integration.support import API_URL, DATABASE_URL

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if API_URL:
        return
    skip = pytest.mark.skip(
        reason="needs the running stack: docker compose --profile tools run --rm --build check"
    )
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(skip)


@pytest.fixture
async def api() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(base_url=API_URL, timeout=10.0) as client:
        yield client


@pytest.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    """A direct database connection, to check what the API does not expose (the outbox,
    processed_messages)."""
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        yield connection
    finally:
        await connection.close()
