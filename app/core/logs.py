"""Logging helpers."""

import logging

# Docker probes these every few seconds; logging each request would bury the useful lines.
HEALTH_CHECK_PATHS = frozenset({"/healthz", "/readyz"})


class HealthCheckAccessFilter(logging.Filter):
    """Drops uvicorn access-log lines for the health probes; every other line passes.

    uvicorn logs each request on the `uvicorn.access` logger with the arguments
    (client address, method, path, HTTP version, status code).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = str(args[2]).split("?", 1)[0]
        return path not in HEALTH_CHECK_PATHS
