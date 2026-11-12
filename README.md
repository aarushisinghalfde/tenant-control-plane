# Tenant Control Plane

An asynchronous tenant provisioning control plane. The HTTP API records tenants and their
provisioning tasks in PostgreSQL; a worker picks the tasks up from RabbitMQ, simulates the
provisioning work, and reports progress back over the broker.

> Work in progress: sections are filled in as each part of the system lands.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Layers and class diagram, startup sequence, step-by-step lifecycle of a request through the outbox to RabbitMQ |
| [docs/evidence/](docs/evidence/README.md) | Manual runs against the real stack, with commands, output and screenshots |
| [Broker outage](docs/evidence/outbox-broker-outage.md) | Writes succeed while RabbitMQ is down; the event is published once it returns |
| [Rejected writes](docs/evidence/rejected-writes.md) | Duplicate slugs, stale versions and invalid transitions are refused and publish nothing |
| <http://localhost:8000/docs> | Interactive API reference (after boot), generated from the code |

## Prerequisites

- Git
- Docker with Compose v2 (Docker Desktop on Windows and macOS)

Nothing else. Python, PostgreSQL and RabbitMQ all run in containers.

## Quick start

```bash
git clone https://github.com/aarushisinghalfde/tenant-control-plane.git && cd tenant-control-plane
docker compose up --build -d --wait                       # build and boot everything
docker compose --profile tools run --rm --build check     # quality gate + tests
```

The API is then on <http://localhost:8000>, with interactive docs at
<http://localhost:8000/docs>. The RabbitMQ management UI is on <http://localhost:15672>
(user `tenants`, password `tenants`; local-development credentials only).

| Purpose | Command |
| --- | --- |
| Boot the stack | `docker compose up --build -d --wait` |
| Follow logs | `docker compose logs -f control-plane` |
| Stop (keep data) | `docker compose down` |
| Stop and delete all data | `docker compose down -v` |
| Quality gate (format, lint, types, security, tests) | `docker compose --profile tools run --rm --build check` |
| Format and auto-fix the code in place | `docker compose --profile tools run --rm --build fmt` |

## Try it

```bash
# Create a tenant: 202 Accepted, returns the tenant and its deploy task
curl -s -X POST localhost:8000/v1/tenants \
  -H 'Content-Type: application/json' \
  -d '{"slug": "acme-corp", "name": "Acme Corp"}'

# List tenants (newest first, paginated with ?limit= and ?cursor=)
curl -s localhost:8000/v1/tenants

# Update the name; `version` must match the tenant's current version
curl -s -X PATCH localhost:8000/v1/tenants/<id> \
  -H 'Content-Type: application/json' \
  -d '{"name": "Acme Corporation", "version": 1}'

# A tenant's tasks
curl -s localhost:8000/v1/tenants/<id>/tasks
```

## API

| Method and path | Purpose | Success |
| --- | --- | --- |
| `POST /v1/tenants` | Create a tenant and its `deploy` task | `202` + `Location` |
| `GET /v1/tenants` | List; filter `?status=`; paginate `?limit=&cursor=` | `200` |
| `GET /v1/tenants/{id}` | Get one tenant | `200` |
| `PATCH /v1/tenants/{id}` | Partial update (`name`) with `version`; creates an `update` task | `202` |
| `DELETE /v1/tenants/{id}` | Start destroying; creates a `destroy` task | `202` |
| `GET /v1/tenants/{id}/tasks` | Tasks of one tenant | `200` |
| `GET /v1/tasks` | List; filter `?tenant_id=&type=&status=`; paginate | `200` |
| `GET /v1/tasks/{id}` | Get one task | `200` |

Every error has the same shape, so clients can branch on `code`. `details` appears only when
specific inputs are involved, and is always a list of `{field, message}`:

```json
{
  "error": {
    "code": "tenant_version_conflict",
    "message": "version 2 is stale; current version is 3",
    "details": [{"field": "version", "message": "must equal current version 3"}]
  }
}
```

Empty results are not errors: a list with no matches returns `200` with
`{"items": [], "next_cursor": null}`, and `next_cursor` is `null` on the last page. A single
resource that does not exist returns `404`.

| Code | HTTP status | When |
| --- | --- | --- |
| `validation_failed` | 400 | Malformed body, query or id; unknown field; invalid cursor |
| `tenant_not_found` | 404 | No tenant with that id |
| `task_not_found` | 404 | No task with that id |
| `tenant_already_exists` | 409 | Slug taken, including by a destroyed tenant |
| `tenant_update_not_allowed` | 409 | PATCH outside `active`; DELETE outside `active` / `failed` |
| `tenant_version_conflict` | 409 | `version` in the PATCH body is not the current version |
| `not_found` / `method_not_allowed` | 404 / 405 | Unknown route or wrong HTTP method |
| `internal_error` | 500 | Anything unexpected (details are logged, never returned) |

## Messaging

Tasks leave the control plane through a **transactional outbox**. A request never talks
to the broker: it writes the tenant, the task and an `outbox` row in one transaction.
A background relay inside the control plane then publishes committed rows and marks
them, so an event goes out only after its write is durable, never on rollback, and is
not lost if the process dies between commit and publish.

```mermaid
sequenceDiagram
    participant C as Client
    participant API as Control plane (API)
    participant DB as PostgreSQL
    participant R as Outbox relay
    participant MQ as RabbitMQ
    C->>API: POST /v1/tenants
    API->>DB: BEGIN; INSERT tenant, task, outbox row; COMMIT
    API-->>C: 202 {tenant, task}
    loop every 200 ms
        R->>DB: SELECT unpublished rows FOR UPDATE SKIP LOCKED
        R->>MQ: publish (persistent, publisher confirm)
        MQ-->>R: ack
        R->>DB: SET published_at; COMMIT
    end
```

- **At-least-once.** If the relay dies after the broker confirms but before it marks the
  row, the event is sent again. The AMQP `message_id` is the task id, so consumers can
  drop duplicates.
- **Retries.** A failed publish increments the row's `attempts`, stops the batch (events
  keep their order) and retries with exponential backoff and jitter, capped at 30 s.
- **Replicas.** `SKIP LOCKED` lets several relays share the outbox without waiting on or
  double-publishing each other's rows.

| Exchange (type) | Routing key | Queue | Direction |
| --- | --- | --- | --- |
| `tasks` (topic) | `task.<type>` | `worker.tasks` | control plane -> worker |
| `task-updates` (topic) | `task.update.<status>` | `controlplane.task-updates` | worker -> control plane |
| `task-updates.dlx` (fanout) | any | `controlplane.task-updates.dlq` | rejected (poison) updates |

All queues are durable quorum queues and all messages are persistent. `/readyz` reports
ready only while both the database and the broker connection are up.

## Tech stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.12 |
| HTTP API | FastAPI + uvicorn |
| Validation | Pydantic v2 |
| Database | PostgreSQL 16 (asyncpg, plain SQL) |
| Broker | RabbitMQ 3.13 (aio-pika) |
| Tests | pytest + pytest-asyncio + httpx |
| Quality gate | ruff (format + lint), mypy (strict), bandit, pip-audit |

## Project layout

The code is layered: each layer only depends on the ones below it, and the domain
depends on nothing.

```text
app/
  main.py                 app factory: wires layers together, startup and shutdown
  api/                    HTTP layer
    routes/                 one router per resource: tenants, tasks, health
    schemas.py              request/response bodies and the error body
    dependencies.py         service lookup, page size, cursor parsing
    error_handlers.py       exceptions -> structured JSON errors, status mapping
    openapi.py              keeps the Swagger docs in line with the real error shape
  services/               use cases, one transaction per state change
    tenant_service.py
    task_service.py
  domain/                 business rules, pure logic with no I/O
    models.py               entities and statuses
    rules.py                tenant and task state machines
    errors.py               domain errors and their API codes
  db/                     persistence
    database.py             pool, migrations, transactions
    repository.py           all SQL
  messaging/              broker integration
    events.py               message formats
    broker.py               RabbitMQ connection, topology, confirmed publishing
    outbox_relay.py         background loop: outbox rows -> broker
  core/                   shared helpers with no business knowledge
    config.py               settings from environment variables
    clock.py                UTC millisecond timestamps
    backoff.py              exponential backoff with jitter
    pagination.py           keyset cursors
migrations/               SQL schema, applied at startup
worker/                   worker simulator (separate process)
tests/
  unit/                   rules, helpers, schemas, events
  api/                    HTTP contract with faked services
scripts/                  quality-gate script
```
