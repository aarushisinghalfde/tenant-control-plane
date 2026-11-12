"""Liveness and readiness probes (used by the container health check)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db.database import Database
from app.messaging.broker import Broker

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: the database answers and the broker connection is up."""
    db: Database = request.app.state.db
    broker: Broker = request.app.state.broker
    checks = {"database": await db.ping(), "broker": broker.is_connected}
    if all(checks.values()):
        return JSONResponse({"status": "ready", **checks})
    return JSONResponse({"status": "unavailable", **checks}, status_code=503)
