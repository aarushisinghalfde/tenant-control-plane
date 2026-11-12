"""Application entry point: builds the FastAPI app and wires its dependencies."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.error_handlers import register_error_handlers
from app.api.openapi import install_openapi
from app.api.routes import health, tasks, tenants
from app.core.config import load_settings
from app.db.database import Database, create_pool, run_migrations
from app.services.task_service import TaskService
from app.services.tenant_service import TenantService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: settings, database pool, migrations, services. Shutdown: close the pool."""
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    pool = await create_pool(settings.database_url)
    try:
        await run_migrations(pool)
        db = Database(pool)
        app.state.db = db
        app.state.tenant_service = TenantService(db)
        app.state.task_service = TaskService(db)
        yield
    finally:
        await pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Tenant Control Plane",
        version="0.1.0",
        description="Asynchronous tenant provisioning: records tenants, queues tasks for "
        "workers and tracks their progress.",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(tenants.router)
    app.include_router(tasks.router)
    app.include_router(health.router)
    install_openapi(app)
    return app


app = create_app()
