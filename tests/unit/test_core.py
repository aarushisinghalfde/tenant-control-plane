"""Unit tests for the shared helpers: timestamps and pagination."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.core.clock import format_timestamp, utc_now
from app.core.pagination import Cursor, paginate

# --- clock ----------------------------------------------------------------------


def test_timestamps_are_utc_with_milliseconds_and_z_suffix() -> None:
    value = datetime(2026, 11, 11, 10, 0, 0, 123456, tzinfo=UTC)
    assert format_timestamp(value) == "2026-11-11T10:00:00.123Z"


def test_timestamps_are_converted_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    value = datetime(2026, 11, 11, 15, 30, 0, tzinfo=ist)
    assert format_timestamp(value) == "2026-11-11T10:00:00.000Z"


def test_utc_now_is_truncated_to_milliseconds() -> None:
    now = utc_now()
    assert now.tzinfo is UTC
    assert now.microsecond % 1000 == 0


# --- cursor ---------------------------------------------------------------------


def test_cursor_round_trips() -> None:
    cursor = Cursor(created_at=datetime(2026, 11, 11, 10, 0, tzinfo=UTC), id=uuid4())
    assert Cursor.decode(cursor.encode()) == cursor


@pytest.mark.parametrize("raw", ["not-base64!!", "e30=", "eyJ0IjogIngiLCAiaWQiOiAieSJ9"])
def test_cursor_decode_rejects_garbage(raw: str) -> None:
    # "e30=" is "{}" (missing keys); the last one has the right keys but bad values.
    with pytest.raises(ValueError, match="invalid cursor"):
        Cursor.decode(raw)


# --- paginate -------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    id: UUID
    created_at: datetime


def _rows(count: int) -> list[Row]:
    start = datetime(2026, 11, 11, tzinfo=UTC)
    return [Row(id=uuid4(), created_at=start - timedelta(minutes=i)) for i in range(count)]


def test_paginate_last_page_has_no_cursor() -> None:
    rows = _rows(3)
    page, cursor = paginate(rows, limit=3)
    assert page == rows
    assert cursor is None


def test_paginate_drops_the_probe_row_and_points_at_the_last_item() -> None:
    rows = _rows(4)  # fetched with limit + 1
    page, cursor = paginate(rows, limit=3)
    assert page == rows[:3]
    assert cursor == Cursor(created_at=rows[2].created_at, id=rows[2].id)
