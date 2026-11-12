"""Shared route dependencies: service lookup, common parameters and cursor parsing."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from app.core.pagination import Cursor
from app.domain.errors import ValidationFailedError
from app.services.task_service import TaskService
from app.services.tenant_service import TenantService


def get_tenant_service(request: Request) -> TenantService:
    service: TenantService = request.app.state.tenant_service
    return service


def get_task_service(request: Request) -> TaskService:
    service: TaskService = request.app.state.task_service
    return service


Tenants = Annotated[TenantService, Depends(get_tenant_service)]
Tasks = Annotated[TaskService, Depends(get_task_service)]

# Parameters shared by several routes, documented once.
TenantId = Annotated[UUID, Path(description="The tenant's `id`.")]
TaskId = Annotated[UUID, Path(description="The task's `id`.")]
Limit = Annotated[int, Query(ge=1, le=200, description="Page size, 1-200.")]
PageCursor = Annotated[
    str | None,
    Query(description="`next_cursor` from the previous page. Omit for the first page."),
]


def decode_cursor(raw: str | None) -> Cursor | None:
    if raw is None:
        return None
    try:
        return Cursor.decode(raw)
    except ValueError as exc:
        raise ValidationFailedError("invalid cursor") from exc
