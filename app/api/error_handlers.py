"""The error contract: every error, expected or not, leaves the API as

    {"error": {"code": "...", "message": "...", "details": ...}}

so clients can branch on `code`. The HTTP status for each code is decided here only, and
the OpenAPI documentation of each error is generated from the same table.
"""

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import SLUG_RULE, ErrorResponse
from app.domain.errors import (
    DomainError,
    TaskNotFoundError,
    TenantAlreadyExistsError,
    TenantNotFoundError,
    TenantUpdateNotAllowedError,
    TenantVersionConflictError,
    ValidationFailedError,
)

logger = logging.getLogger(__name__)

HTTP_STATUS: Mapping[type[DomainError], int] = {
    ValidationFailedError: 400,
    TenantNotFoundError: 404,
    TaskNotFoundError: 404,
    TenantAlreadyExistsError: 409,
    TenantUpdateNotAllowedError: 409,
    TenantVersionConflictError: 409,
}


def _version_detail(current: int) -> dict[str, str]:
    return {
        "field": "version",
        "message": f"must equal current version {current}",
    }


# One realistic body per code, shown as the example in the Swagger docs.
_EXAMPLES: Mapping[str, dict[str, Any]] = {
    ValidationFailedError.code: {
        "code": "validation_failed",
        "message": "request is invalid",
        "details": [{"field": "body.slug", "message": SLUG_RULE}],
    },
    TenantNotFoundError.code: {
        "code": "tenant_not_found",
        "message": "tenant 3fa85f64-5717-4562-b3fc-2c963f66afa6 not found",
    },
    TaskNotFoundError.code: {
        "code": "task_not_found",
        "message": "task 3fa85f64-5717-4562-b3fc-2c963f66afa6 not found",
    },
    TenantAlreadyExistsError.code: {
        "code": "tenant_already_exists",
        "message": "slug 'acme-corp' is already taken",
    },
    TenantUpdateNotAllowedError.code: {
        "code": "tenant_update_not_allowed",
        "message": "cannot update tenant in status provisioning; requires active",
    },
    TenantVersionConflictError.code: {
        "code": "tenant_version_conflict",
        "message": "version 2 is stale; current version is 3",
        "details": [_version_detail(current=3)],
    },
}


def error_responses(*errors: type[DomainError]) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` for the errors a route can return, grouped by HTTP status.

    Example: responses=error_responses(TenantNotFoundError, TenantVersionConflictError)
    """
    by_status: dict[int, list[type[DomainError]]] = {}
    for error in errors:
        by_status.setdefault(HTTP_STATUS[error], []).append(error)
    responses: dict[int | str, dict[str, Any]] = {}
    for status, group in by_status.items():
        examples = {error.code: {"value": {"error": _EXAMPLES[error.code]}} for error in group}
        responses[status] = {
            "model": ErrorResponse,
            "description": " or ".join(f"`{error.code}`" for error in group),
            "content": {"application/json": {"examples": examples}},
        }
    return responses


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        details = (
            [_version_detail(exc.current_version)]
            if isinstance(exc, TenantVersionConflictError)
            else None
        )
        return error_response(HTTP_STATUS.get(type(exc), 400), exc.code, exc.message, details)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": str(error["msg"]).removeprefix("Value error, "),
            }
            for error in exc.errors()
        ]
        return error_response(400, "validation_failed", "request is invalid", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "internal server error")
