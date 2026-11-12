"""Liveness and readiness probes (used by the container health check)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db.database import Database

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: dependencies are reachable."""
    db: Database = request.app.state.db
    if await db.ping():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "unavailable"}, status_code=503)
