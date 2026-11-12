"""/v1/tenants: create, list, get, partial update and delete, plus a tenant's tasks.

Mutations return 202 Accepted: the change is recorded, the provisioning work happens
asynchronously, and the returned task is how the client follows it.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.dependencies import Limit, PageCursor, Tasks, TenantId, Tenants, decode_cursor
from app.api.error_handlers import error_responses
from app.api.routes.tasks import TaskStatusFilter, TaskTypeFilter, task_page
from app.api.schemas import (
    TaskPage,
    TenantAccepted,
    TenantCreate,
    TenantOut,
    TenantPage,
    TenantUpdate,
)
from app.domain.errors import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    TenantUpdateNotAllowedError,
    TenantVersionConflictError,
    ValidationFailedError,
)
from app.domain.models import TenantStatus

router = APIRouter(
    prefix="/v1/tenants",
    tags=["tenants"],
    responses=error_responses(ValidationFailedError),
)


@router.post(
    "",
    status_code=202,
    summary="Create a tenant",
    responses=error_responses(TenantAlreadyExistsError),
)
async def create_tenant(body: TenantCreate, tenants: Tenants, response: Response) -> TenantAccepted:
    """Records a new tenant in `provisioning` and queues a `deploy` task for a worker.

    The `Location` header points at the new tenant. It becomes `active` when the deploy
    task finishes, or `failed` if it fails. A slug that is already taken, including by a
    destroyed tenant, is rejected with 409 `tenant_already_exists`.
    """
    tenant, task = await tenants.create(body.slug, body.name)
    response.headers["Location"] = f"/v1/tenants/{tenant.id}"
    return TenantAccepted.of(tenant, task)


@router.get("", summary="List tenants")
async def list_tenants(
    tenants: Tenants,
    status: Annotated[
        TenantStatus | None, Query(description="Only tenants in this status.")
    ] = None,
    limit: Limit = 50,
    cursor: PageCursor = None,
) -> TenantPage:
    """All tenants, newest first, including destroyed ones."""
    items, next_cursor = await tenants.list_tenants(
        status=status, cursor=decode_cursor(cursor), limit=limit
    )
    return TenantPage(
        items=[TenantOut.model_validate(tenant) for tenant in items],
        next_cursor=next_cursor.encode() if next_cursor else None,
    )


@router.get("/{tenant_id}", summary="Get a tenant", responses=error_responses(TenantNotFoundError))
async def get_tenant(tenant_id: TenantId, tenants: Tenants) -> TenantOut:
    """One tenant, including its current `status` and `version`."""
    return TenantOut.model_validate(await tenants.get(tenant_id))


@router.patch(
    "/{tenant_id}",
    status_code=202,
    summary="Update a tenant",
    responses=error_responses(
        TenantNotFoundError, TenantVersionConflictError, TenantUpdateNotAllowedError
    ),
)
async def update_tenant(
    tenant_id: TenantId, body: TenantUpdate, tenants: Tenants
) -> TenantAccepted:
    """Changes an `active` tenant and queues an `update` task; the tenant moves to `updating`.

    `version` must equal the tenant's current version, so two clients editing at once
    cannot silently overwrite each other: the second gets 409 `tenant_version_conflict`.
    """
    tenant, task = await tenants.update(tenant_id, name=body.name, expected_version=body.version)
    return TenantAccepted.of(tenant, task)


@router.delete(
    "/{tenant_id}",
    status_code=202,
    summary="Delete a tenant",
    responses=error_responses(TenantNotFoundError, TenantUpdateNotAllowedError),
)
async def delete_tenant(tenant_id: TenantId, tenants: Tenants) -> TenantAccepted:
    """Starts destroying an `active` or `failed` tenant by queueing a `destroy` task.

    The tenant moves to `destroying`, then `destroyed`. The record is kept, so its slug
    can never be reused.
    """
    tenant, task = await tenants.delete(tenant_id)
    return TenantAccepted.of(tenant, task)


@router.get(
    "/{tenant_id}/tasks",
    summary="List a tenant's tasks",
    responses=error_responses(TenantNotFoundError),
)
async def list_tenant_tasks(
    tenant_id: TenantId,
    tenants: Tenants,
    tasks: Tasks,
    status: TaskStatusFilter = None,
    task_type: TaskTypeFilter = None,
    limit: Limit = 50,
    cursor: PageCursor = None,
) -> TaskPage:
    """The tenant's task history, newest first: its deploy, updates and destroy."""
    await tenants.get(tenant_id)  # 404 if the tenant does not exist
    return await task_page(tasks, tenant_id, task_type, status, cursor, limit)
