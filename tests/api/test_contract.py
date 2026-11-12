"""HTTP contract tests: status codes and the structured error body.

The services are replaced with in-memory fakes, so these run without a database.
End-to-end behaviour against real Postgres and RabbitMQ is covered by the
integration tests.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_task_service, get_tenant_service
from app.domain.errors import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    TenantUpdateNotAllowedError,
    TenantVersionConflictError,
)
from app.domain.models import Task, TaskStatus, TaskType, Tenant, TenantStatus
from app.main import app

NOW = datetime(2026, 11, 11, 10, 0, 0, 123000, tzinfo=UTC)
KNOWN_ID = UUID("00000000-0000-0000-0000-000000000001")


def _tenant(status: TenantStatus = TenantStatus.PROVISIONING, version: int = 1) -> Tenant:
    return Tenant(
        id=KNOWN_ID,
        slug="acme",
        name="Acme",
        status=status,
        version=version,
        created_at=NOW,
        updated_at=NOW,
    )


def _task(task_type: TaskType = TaskType.DEPLOY) -> Task:
    return Task(
        id=uuid4(),
        tenant_id=KNOWN_ID,
        type=task_type,
        status=TaskStatus.ACCEPTED,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeTenantService:
    """Knows one tenant (KNOWN_ID, version 1, active); the slug "taken" already exists."""

    async def create(self, slug: str, name: str) -> tuple[Tenant, Task]:
        if slug == "taken":
            raise TenantAlreadyExistsError(f"slug {slug!r} is already taken")
        return _tenant(), _task()

    async def list_tenants(self, **_filters: object) -> tuple[list[Tenant], None]:
        return [], None

    async def get(self, tenant_id: UUID) -> Tenant:
        if tenant_id != KNOWN_ID:
            raise TenantNotFoundError(f"tenant {tenant_id} not found")
        return _tenant(TenantStatus.ACTIVE)

    async def update(
        self, tenant_id: UUID, *, name: str | None, expected_version: int
    ) -> tuple[Tenant, Task]:
        await self.get(tenant_id)
        if expected_version != 1:
            raise TenantVersionConflictError(current_version=1, requested_version=expected_version)
        return _tenant(TenantStatus.UPDATING, version=2), _task(TaskType.UPDATE)

    async def delete(self, tenant_id: UUID) -> tuple[Tenant, Task]:
        await self.get(tenant_id)
        raise TenantUpdateNotAllowedError("tenant is updating")


class FakeTaskService:
    async def list_tasks(self, **_filters: object) -> tuple[list[Task], None]:
        return [], None


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_tenant_service] = FakeTenantService
    app.dependency_overrides[get_task_service] = FakeTaskService
    # Not used as a context manager, so the lifespan (database startup) does not run.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_returns_202_with_location_and_task(client: TestClient) -> None:
    response = client.post("/v1/tenants", json={"slug": "acme", "name": "Acme"})

    assert response.status_code == 202
    assert response.headers["location"] == f"/v1/tenants/{KNOWN_ID}"
    body = response.json()
    assert body["tenant"]["status"] == "provisioning"
    assert body["tenant"]["created_at"] == "2026-11-11T10:00:00.123Z"
    assert body["task"]["type"] == "deploy"
    assert body["task"]["status"] == "accepted"
    assert body["task"]["error"] is None  # always present, null unless the task failed


def test_duplicate_slug_is_409_tenant_already_exists(client: TestClient) -> None:
    response = client.post("/v1/tenants", json={"slug": "taken", "name": "Acme"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "tenant_already_exists"


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "Not Valid", "name": "Acme"},
        {"slug": "acme", "name": ""},
        {"slug": "acme"},
        {"slug": "acme", "name": "Acme", "unexpected": True},
        {},
        {"slug": None, "name": "Acme"},
        {"slug": "", "name": "Acme"},
        {"slug": "acme", "name": "   "},
    ],
)
def test_invalid_create_body_is_400_validation_failed(client: TestClient, body: object) -> None:
    response = client.post("/v1/tenants", json=body)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["details"]
    assert all(not d["message"].startswith("Value error") for d in error["details"])


def test_unknown_tenant_is_404_tenant_not_found(client: TestClient) -> None:
    response = client.get(f"/v1/tenants/{uuid4()}")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "tenant_not_found"
    assert "details" not in error  # only present when specific fields are involved


def test_create_without_a_body_is_400(client: TestClient) -> None:
    response = client.post("/v1/tenants")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_tasks_of_unknown_tenant_is_404(client: TestClient) -> None:
    response = client.get(f"/v1/tenants/{uuid4()}/tasks")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tenant_not_found"


def test_malformed_id_is_400(client: TestClient) -> None:
    response = client.get("/v1/tenants/not-a-uuid")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_stale_version_is_409_with_field_detail(client: TestClient) -> None:
    response = client.patch(f"/v1/tenants/{KNOWN_ID}", json={"name": "New", "version": 7})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "tenant_version_conflict"
    assert error["message"] == "version 7 is stale; current version is 1"
    assert error["details"] == [{"field": "version", "message": "must equal current version 1"}]


@pytest.mark.parametrize("body", [{"name": "New"}, {"name": "New", "version": None}])
def test_patch_without_version_is_400(client: TestClient, body: object) -> None:
    response = client.patch(f"/v1/tenants/{KNOWN_ID}", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("body", [{"version": 1}, {"name": None, "version": 1}])
def test_patch_without_changes_is_400(client: TestClient, body: object) -> None:
    response = client.patch(f"/v1/tenants/{KNOWN_ID}", json=body)

    assert response.status_code == 400
    details = response.json()["error"]["details"]
    assert details[0]["message"] == "provide at least one field to update"


def test_patch_success_is_202_with_update_task(client: TestClient) -> None:
    response = client.patch(f"/v1/tenants/{KNOWN_ID}", json={"name": "New", "version": 1})

    assert response.status_code == 202
    assert response.json()["tenant"]["version"] == 2
    assert response.json()["task"]["type"] == "update"


def test_delete_in_wrong_state_is_409_tenant_update_not_allowed(client: TestClient) -> None:
    response = client.delete(f"/v1/tenants/{KNOWN_ID}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "tenant_update_not_allowed"


@pytest.mark.parametrize("path", ["/v1/tenants", "/v1/tasks"])
def test_empty_list_is_200_with_no_items_and_null_cursor(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": ""},
        {"cursor": "garbage!"},
        {"limit": "0"},
        {"limit": "201"},
        {"status": ""},
        {"status": "bogus"},
        {"type": "bogus"},
    ],
)
def test_invalid_query_parameters_are_400(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/v1/tasks", params=params)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_unknown_route_uses_the_same_error_shape(client: TestClient) -> None:
    response = client.get("/v1/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- OpenAPI document -------------------------------------------------------------


def test_openapi_documents_the_real_error_shape(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    patch = schema["paths"]["/v1/tenants/{tenant_id}"]["patch"]["responses"]

    assert "422" not in patch  # FastAPI's default validation response is not what we return
    assert set(patch) >= {"202", "400", "404", "409"}
    conflict = patch["409"]["content"]["application/json"]
    assert conflict["schema"]["$ref"].endswith("/ErrorResponse")
    assert set(conflict["examples"]) == {"tenant_version_conflict", "tenant_update_not_allowed"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]
