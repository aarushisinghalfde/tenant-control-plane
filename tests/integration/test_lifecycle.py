"""End to end: the API, the outbox relay, RabbitMQ, the worker and the consumer together.

These are the brief's acceptance path: a new tenant walks its whole lifecycle with no
manual step, observable through the tenant and task endpoints.
"""

import httpx2

from tests.integration.support import (
    active_tenant,
    create_tenant,
    publish_update,
    wait_for_status,
)


async def test_new_tenant_is_deployed_with_no_manual_steps(api: httpx2.AsyncClient) -> None:
    tenant, task = await create_tenant(api)
    assert (tenant["status"], tenant["version"]) == ("provisioning", 1)
    assert (task["type"], task["status"]) == ("deploy", "accepted")

    active = await wait_for_status(api, tenant["id"], "active")

    assert active["version"] == 2  # the worker-driven change bumps the version too
    finished = (await api.get(f"/v1/tasks/{task['id']}")).json()
    assert (finished["status"], finished["error"]) == ("done", None)


async def test_update_then_delete_walks_the_rest_of_the_lifecycle(
    api: httpx2.AsyncClient,
) -> None:
    tenant = await active_tenant(api)  # version 2
    tenant_url = f"/v1/tenants/{tenant['id']}"

    patched = await api.patch(tenant_url, json={"name": "Renamed", "version": 2})
    assert patched.status_code == 202, patched.text
    assert (patched.json()["tenant"]["status"], patched.json()["tenant"]["version"]) == (
        "updating",
        3,
    )
    updated = await wait_for_status(api, tenant["id"], "active")
    assert (updated["name"], updated["version"]) == ("Renamed", 4)

    deleted = await api.delete(tenant_url)
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["tenant"]["status"] == "destroying"
    destroyed = await wait_for_status(api, tenant["id"], "destroyed")
    assert destroyed["version"] == 6

    history = (await api.get(f"{tenant_url}/tasks")).json()["items"]
    assert [(t["type"], t["status"]) for t in history] == [
        ("destroy", "done"),
        ("update", "done"),
        ("deploy", "done"),
    ]


async def test_slug_of_a_destroyed_tenant_stays_taken(api: httpx2.AsyncClient) -> None:
    tenant = await active_tenant(api)
    await api.delete(f"/v1/tenants/{tenant['id']}")
    await wait_for_status(api, tenant["id"], "destroyed")

    again = await api.post("/v1/tenants", json={"slug": tenant["slug"], "name": "Again"})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "tenant_already_exists"


async def test_failed_deploy_fails_the_tenant_which_can_still_be_destroyed(
    api: httpx2.AsyncClient,
) -> None:
    tenant, task = await create_tenant(api)
    # Published straight away, so it lands before the worker's own updates (the worker
    # waits at least 0.5 s per step); its later `done` is then stale and ignored.
    await publish_update(task, "failed", error="disk full")

    failed = await wait_for_status(api, tenant["id"], "failed")
    assert failed["version"] == 2
    finished = (await api.get(f"/v1/tasks/{task['id']}")).json()
    assert (finished["status"], finished["error"]) == ("failed", "disk full")

    deleted = await api.delete(f"/v1/tenants/{tenant['id']}")
    assert deleted.status_code == 202, deleted.text
    await wait_for_status(api, tenant["id"], "destroyed")
