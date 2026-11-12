"""OpenAPI (Swagger) document that matches what the API really returns.

FastAPI documents a 422 `HTTPValidationError` (with `loc`/`msg`/`type`) on every route
by default. This API returns 400 with the shared error body instead, so the default
entries are removed and each router documents its real errors.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def build_openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes
    )
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            operation.get("responses", {}).pop("422", None)
    components = schema.get("components", {}).get("schemas", {})
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)
    app.openapi_schema = schema
    return schema


def install_openapi(app: FastAPI) -> None:
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
