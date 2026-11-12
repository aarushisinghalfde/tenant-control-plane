"""Time helpers. The API stores and exposes UTC timestamps with millisecond precision."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Current UTC time truncated to milliseconds, so stored and returned values agree."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def format_timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with millisecond precision and a `Z` suffix."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Use in Pydantic models for any timestamp that leaves the service.
Timestamp = Annotated[datetime, PlainSerializer(format_timestamp, return_type=str)]
