"""Task queries. Read-only: tasks are created by TenantService and driven by the worker."""

from uuid import UUID

from app.core.pagination import Cursor, paginate
from app.db.database import Database
from app.domain.errors import TaskNotFoundError
from app.domain.models import Task, TaskStatus, TaskType


class TaskService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, task_id: UUID) -> Task:
        async with self._db.read() as repo:
            task = await repo.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        return task

    async def list_tasks(
        self,
        *,
        tenant_id: UUID | None,
        task_type: TaskType | None,
        status: TaskStatus | None,
        cursor: Cursor | None,
        limit: int,
    ) -> tuple[list[Task], Cursor | None]:
        after = (cursor.created_at, cursor.id) if cursor else None
        async with self._db.read() as repo:
            rows = await repo.list_tasks(
                tenant_id=tenant_id,
                task_type=task_type,
                status=status,
                after=after,
                limit=limit + 1,
            )
        return paginate(rows, limit)
