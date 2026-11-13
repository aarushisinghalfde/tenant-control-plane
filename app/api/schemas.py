"""HTTP request and response bodies, including the structured error body.

Kept apart from the domain so the API contract can evolve without touching the rules.
Field descriptions here are what the Swagger page at /docs shows.
"""

from __future__ import annotations

from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.clock import Timestamp
from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.domain.rules import is_valid_slug

TenantName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

SLUG_RULE = (
    "must be 3-28 chars: lowercase letters, digits, hyphens; "
    "start with a letter, end with a letter or digit"
)
SLUG_HELP = (
    "Unique, URL-safe identifier chosen at creation, e.g. `acme-corp`. 3-28 lowercase letters, "
    "digits or hyphens; starts with a letter, ends with a letter or digit (Kubernetes namespace "
    "rules). Never changes and is never reused, even after the tenant is destroyed."
)
VERSION_HELP = (
    "Increments by 1 on every change to the tenant, including status changes applied by the "
    "worker. Send it back in PATCH as the optimistic lock."
)
TENANT_STATUS_HELP = (
    "Lifecycle state: `provisioning` (being set up), `active` (ready), `updating`, "
    "`destroying`, `destroyed` (gone, record kept) or `failed` (last task failed; only DELETE "
    "is allowed)."
)
TASK_STATUS_HELP = (
    "`accepted` (queued) -> `in_progress` -> `done` or `failed`. `done` and `failed` are final."
)
TIMESTAMP_HELP = "UTC, ISO 8601 with milliseconds."
NEXT_CURSOR_HELP = (
    "Opaque bookmark: send it as `?cursor=` to get the next page. `null` on the last page."
)

# --- requests -----------------------------------------------------------------


class _Request(BaseModel):
    """Request bodies reject unknown fields, so typos fail loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


class TenantCreate(_Request):
    """Body of POST /v1/tenants."""

    slug: str = Field(description=SLUG_HELP, examples=["acme-corp"])
    name: TenantName = Field(
        description="Human-readable display name; any text, 1-200 characters.",
        examples=["Acme Corp"],
    )

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, value: str) -> str:
        if not is_valid_slug(value):
            raise ValueError(SLUG_RULE)
        return value


class TenantUpdate(_Request):
    """Body of PATCH /v1/tenants/{tenant_id}: send only the fields to change, plus `version`."""

    name: TenantName | None = Field(
        default=None, description="New display name.", examples=["Acme Corporation"]
    )
    version: int = Field(
        ge=1,
        description=(
            "The tenant's current `version`, from your last read. If the tenant changed since, "
            "the request fails with 409 `tenant_version_conflict` instead of overwriting. "
            "A newly created tenant is at version 2 once it is `active`."
        ),
        examples=[2],
    )

    @model_validator(mode="after")
    def _has_changes(self) -> Self:
        if self.name is None:
            raise ValueError("provide at least one field to update")
        return self


# --- responses ----------------------------------------------------------------


class _Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TenantOut(_Response):
    """A tenant: one customer environment."""

    id: UUID = Field(description="Server-generated identifier; use it in URLs.")
    slug: str = Field(description=SLUG_HELP)
    name: str = Field(description="Human-readable display name.")
    status: TenantStatus = Field(description=TENANT_STATUS_HELP)
    version: int = Field(description=VERSION_HELP)
    created_at: Timestamp = Field(description=TIMESTAMP_HELP)
    updated_at: Timestamp = Field(description=TIMESTAMP_HELP)


class TaskOut(_Response):
    """A task: one asynchronous operation on a tenant, carried out by a worker."""

    id: UUID = Field(description="Task identifier; also the id of the message sent to the worker.")
    type: TaskType = Field(
        description="`deploy` (new tenant), `update` (PATCH) or `destroy` (DELETE)."
    )
    tenant_id: UUID = Field(description="The `id` of the tenant this task acts on.")
    status: TaskStatus = Field(description=TASK_STATUS_HELP)
    error: str | None = Field(description="Why the task failed; `null` unless status is `failed`.")
    created_at: Timestamp = Field(description=TIMESTAMP_HELP)
    updated_at: Timestamp = Field(description=TIMESTAMP_HELP)


class TenantAccepted(BaseModel):
    """Body of a 202 response: the change is recorded and a task will carry it out."""

    tenant: TenantOut = Field(description="The tenant as it was just written.")
    task: TaskOut = Field(description="The task created for this change; poll it to follow it.")

    @classmethod
    def of(cls, tenant: Tenant, task: Task) -> TenantAccepted:
        return cls(tenant=TenantOut.model_validate(tenant), task=TaskOut.model_validate(task))


class TenantPage(BaseModel):
    """One page of tenants, newest first."""

    items: list[TenantOut] = Field(description="Up to `limit` tenants.")
    next_cursor: str | None = Field(description=NEXT_CURSOR_HELP)


class TaskPage(BaseModel):
    """One page of tasks, newest first."""

    items: list[TaskOut] = Field(description="Up to `limit` tasks.")
    next_cursor: str | None = Field(description=NEXT_CURSOR_HELP)


# --- errors -------------------------------------------------------------------


class ErrorDetail(BaseModel):
    field: str = Field(description="The input the problem relates to, e.g. `body.slug`.")
    message: str = Field(description="What is wrong with that input.")


class ErrorBody(BaseModel):
    code: str = Field(description="Stable, machine-readable error code. Clients branch on this.")
    message: str = Field(description="Human-readable explanation of what went wrong.")
    details: list[ErrorDetail] | None = Field(
        default=None,
        description="Per-field problems, when the error relates to specific inputs.",
    )


class ErrorResponse(BaseModel):
    """The single error shape every endpoint returns."""

    error: ErrorBody
