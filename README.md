# Tenant Control Plane

An asynchronous tenant provisioning control plane. The HTTP API records tenants and their
provisioning tasks in PostgreSQL; a worker picks the tasks up from RabbitMQ, simulates the
provisioning work, and reports progress back over the broker.

> Work in progress: sections are filled in as each part of the system lands.

## Prerequisites

- Git
- Docker with Compose v2 (Docker Desktop on Windows and macOS)

Nothing else. Python, PostgreSQL and RabbitMQ all run in containers.

## Commands

| Purpose | Command |
| --- | --- |
| Run the quality gate (format, lint, types, security) | `docker compose --profile tools run --rm --build check` |
| Format and auto-fix the code in place | `docker compose --profile tools run --rm --build fmt` |

The boot and test commands are added once the stack exists.

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

```text
app/        control plane: API, domain rules, database, outbox relay, consumer
worker/     worker simulator (separate process)
tests/      unit and integration tests
scripts/    developer and quality-gate scripts
```
