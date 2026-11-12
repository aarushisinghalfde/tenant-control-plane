"""Keyset pagination helpers.

A cursor is the (created_at, id) of the last item on a page. Unlike page numbers it
stays correct while new rows are inserted, and the database jumps straight to it
through an index instead of counting past skipped rows.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: UUID

    def encode(self) -> str:
        """Opaque to clients: base64url of a small JSON object."""
        payload = json.dumps({"t": self.created_at.isoformat(), "id": str(self.id)})
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @classmethod
    def decode(cls, raw: str) -> Cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode()))
            return cls(created_at=datetime.fromisoformat(data["t"]), id=UUID(data["id"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid cursor") from exc


class Paginated(Protocol):
    """Anything ordered by (created_at, id) can be paginated."""

    @property
    def id(self) -> UUID: ...

    @property
    def created_at(self) -> datetime: ...


def paginate[T: Paginated](rows: list[T], limit: int) -> tuple[list[T], Cursor | None]:
    """Split rows fetched with `limit + 1` into one page and the cursor for the next.

    The extra row only signals that another page exists; it is not returned.
    """
    if len(rows) <= limit:
        return rows, None
    page = rows[:limit]
    return page, Cursor(created_at=page[-1].created_at, id=page[-1].id)
