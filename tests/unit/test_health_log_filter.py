"""The access-log filter hides health-probe requests and keeps everything else."""

import logging

import pytest

from app.core.logs import HealthCheckAccessFilter


def _access_record(path: str) -> logging.LogRecord:
    """A record shaped like the ones uvicorn writes to the `uvicorn.access` logger."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.18.0.1:5000", "GET", path, "1.1", 200),
        exc_info=None,
    )


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_probe_requests_are_dropped(path: str) -> None:
    assert HealthCheckAccessFilter().filter(_access_record(path)) is False


def test_health_probe_with_query_string_is_dropped() -> None:
    assert HealthCheckAccessFilter().filter(_access_record("/readyz?verbose=1")) is False


@pytest.mark.parametrize("path", ["/v1/tenants", "/docs"])
def test_other_requests_are_kept(path: str) -> None:
    assert HealthCheckAccessFilter().filter(_access_record(path)) is True


def test_records_without_request_arguments_are_kept() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message",
        args=None,
        exc_info=None,
    )
    assert HealthCheckAccessFilter().filter(record) is True
