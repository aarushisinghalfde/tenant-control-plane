"""Domain errors. Each carries a `code` that is part of the public API contract."""

from typing import ClassVar


class DomainError(Exception):
    """Base for errors the API turns into a structured response; clients branch on `code`."""

    code: ClassVar[str]

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TenantNotFoundError(DomainError):
    code = "tenant_not_found"


class TenantAlreadyExistsError(DomainError):
    code = "tenant_already_exists"


class TenantUpdateNotAllowedError(DomainError):
    code = "tenant_update_not_allowed"


class TenantVersionConflictError(DomainError):
    code = "tenant_version_conflict"

    def __init__(self, current_version: int, requested_version: int) -> None:
        super().__init__(
            f"version {requested_version} is stale; current version is {current_version}"
        )
        self.current_version = current_version
        self.requested_version = requested_version


class TaskNotFoundError(DomainError):
    code = "task_not_found"


class ValidationFailedError(DomainError):
    """Input that passed schema validation but is still unusable, e.g. a garbled cursor."""

    code = "validation_failed"
