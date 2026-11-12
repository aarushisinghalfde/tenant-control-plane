"""/v1/tasks: read-only. Tasks are created by the tenant routes and driven by the worker."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import Limit, PageCursor, TaskId, Tasks, decode_cursor
from app.api.error_handlers import error_responses
from app.api.schemas import TaskOut, TaskPage
from app.domain.errors import TaskNotFoundError, ValidationFailedError
from app.domain.models import TaskStatus, TaskType
from app.services.task_service import TaskService

router = APIRouter(
    prefix="/v1/tasks",
    tags=["tasks"],
    responses=error_responses(ValidationFailedError),
)

TaskStatusFilter = Annotated[TaskStatus | None, Query(description="Only tasks in this status.")]
TaskTypeFilter = Annotated[
    TaskType | None, Query(alias="type", description="Only tasks of this type.")
]


@router.get("", summary="List tasks")
async def list_tasks(
    tasks: Tasks,
    tenant_id: Annotated[
        UUID | None, Query(description="Only tasks of the tenant with this `id`.")
    ] = None,
    status: TaskStatusFilter = None,
    task_type: TaskTypeFilter = None,
    limit: Limit = 50,
    cursor: PageCursor = None,
) -> TaskPage:
    """All tasks, newest first. Filters can be combined."""
    return await task_page(tasks, tenant_id, task_type, status, cursor, limit)


@router.get("/{task_id}", summary="Get a task", responses=error_responses(TaskNotFoundError))
async def get_task(task_id: TaskId, tasks: Tasks) -> TaskOut:
    """One task. Poll this to follow an operation started by POST, PATCH or DELETE."""
    return TaskOut.model_validate(await tasks.get(task_id))


async def task_page(
    tasks: TaskService,
    tenant_id: UUID | None,
    task_type: TaskType | None,
    status: TaskStatus | None,
    cursor: str | None,
    limit: int,
) -> TaskPage:
    """Shared by /v1/tasks and /v1/tenants/{id}/tasks."""
    items, next_cursor = await tasks.list_tasks(
        tenant_id=tenant_id,
        task_type=task_type,
        status=status,
        cursor=decode_cursor(cursor),
        limit=limit,
    )
    return TaskPage(
        items=[TaskOut.model_validate(task) for task in items],
        next_cursor=next_cursor.encode() if next_cursor else None,
    )
