"""Races decided by the database: unique slugs and the guarded, versioned UPDATE.

Each test fires many identical requests at once and checks that exactly one wins and
that the losers left nothing behind (no tenant, no task, no event).
"""

import asyncio
from uuid import UUID

import asyncpg
import httpx2

from tests.integration.support import active_tenant, new_slug, wait_for_status


async def test_concurrent_creates_with_one_slug_have_exactly_one_winner(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    slug = new_slug()

    responses = await asyncio.gather(
        *(api.post("/v1/tenants", json={"slug": slug, "name": f"Racer {i}"}) for i in range(20))
    )

    codes = [r.status_code for r in responses]
    assert codes.count(202) == 1, codes
    assert codes.count(409) == 19, codes
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "tenant_already_exists"
    }
    assert await db.fetchval("SELECT count(*) FROM tenants WHERE slug = $1", slug) == 1
    events = "SELECT count(*) FROM outbox WHERE payload -> 'tenant' ->> 'slug' = $1"
    assert await db.fetchval(events, slug) == 1


async def test_concurrent_patches_with_one_version_have_exactly_one_winner(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    tenant = await active_tenant(api)

    responses = await asyncio.gather(
        *(
            api.patch(
                f"/v1/tenants/{tenant['id']}",
                json={"name": f"Name {i}", "version": tenant["version"]},
            )
            for i in range(50)
        )
    )

    winners = [r for r in responses if r.status_code == 202]
    losers = [r for r in responses if r.status_code != 202]
    assert len(winners) == 1, [r.status_code for r in responses]
    assert {r.status_code for r in losers} == {409}
    # A loser either saw the bumped version (stale) or the tenant already `updating`.
    assert {r.json()["error"]["code"] for r in losers} <= {
        "tenant_version_conflict",
        "tenant_update_not_allowed",
    }
    update_tasks = "SELECT count(*) FROM tasks WHERE tenant_id = $1 AND type = 'update'"
    assert await db.fetchval(update_tasks, UUID(tenant["id"])) == 1

    final = await wait_for_status(api, tenant["id"], "active")
    assert final["name"] == winners[0].json()["tenant"]["name"]


async def test_concurrent_deletes_have_exactly_one_winner(
    api: httpx2.AsyncClient, db: asyncpg.Connection
) -> None:
    tenant = await active_tenant(api)

    responses = await asyncio.gather(
        *(api.delete(f"/v1/tenants/{tenant['id']}") for _ in range(10))
    )

    codes = [r.status_code for r in responses]
    assert codes.count(202) == 1, codes
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "tenant_update_not_allowed"
    }
    destroy_tasks = "SELECT count(*) FROM tasks WHERE tenant_id = $1 AND type = 'destroy'"
    assert await db.fetchval(destroy_tasks, UUID(tenant["id"])) == 1
