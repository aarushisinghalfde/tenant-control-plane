"""Worker settings: command-line flags, each with an environment-variable twin.

A flag wins over its variable, and the variable wins over the default. The
variables let `docker compose` set a scenario without changing the command, for
example `WORKER_FAIL_RATE=1`.
"""

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    rabbitmq_url: str
    fail_rate: float  # chance a task ends `failed` instead of `done`
    min_delay_ms: int  # each step waits a random time in [min, max]
    max_delay_ms: int
    duplicate_rate: float  # chance each update is sent twice, same message_id
    reorder: bool  # send the outcome first, then a stale `in_progress`
    seed: int | None  # fixes the random choices, for repeatable runs
    concurrency: int  # tasks handled at once (the broker prefetch)
    log_level: str


def parse_config(argv: Sequence[str], env: Mapping[str, str]) -> WorkerConfig:
    """Build the settings; prints usage and exits with status 2 on a bad value."""

    def default(name: str, fallback: str | None) -> str | None:
        value = env.get(name)
        return value if value else fallback  # an empty variable counts as unset

    parser = argparse.ArgumentParser(
        prog="python -m worker.main",
        description="Simulates provisioning: consumes tasks and reports progress.",
    )
    # argparse runs `type` on string defaults too, so variables are checked like flags.
    parser.add_argument(
        "--rabbitmq-url",
        default=default("RABBITMQ_URL", None),
        help="AMQP URL (env RABBITMQ_URL, required)",
    )
    parser.add_argument(
        "--fail-rate",
        type=_rate,
        default=default("WORKER_FAIL_RATE", "0"),
        help="0-1, chance a task fails (env WORKER_FAIL_RATE, default 0)",
    )
    parser.add_argument(
        "--min-delay-ms",
        type=_non_negative_int,
        default=default("WORKER_MIN_DELAY_MS", "500"),
        help="shortest wait per step (env WORKER_MIN_DELAY_MS, default 500)",
    )
    parser.add_argument(
        "--max-delay-ms",
        type=_non_negative_int,
        default=default("WORKER_MAX_DELAY_MS", "2000"),
        help="longest wait per step (env WORKER_MAX_DELAY_MS, default 2000)",
    )
    parser.add_argument(
        "--duplicate-rate",
        type=_rate,
        default=default("WORKER_DUPLICATE_RATE", "0"),
        help="0-1, chance each update is sent twice (env WORKER_DUPLICATE_RATE, default 0)",
    )
    parser.add_argument(
        "--reorder",
        type=_flag,
        nargs="?",
        const="true",
        default=default("WORKER_REORDER", "false"),
        help="send the outcome before in_progress (env WORKER_REORDER, default false)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=default("WORKER_SEED", None),
        help="random seed for repeatable runs (env WORKER_SEED)",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=default("WORKER_CONCURRENCY", "4"),
        help="tasks handled at once (env WORKER_CONCURRENCY, default 4)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=default("LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    if not args.rabbitmq_url:
        parser.error("--rabbitmq-url or RABBITMQ_URL is required")
    if args.min_delay_ms > args.max_delay_ms:
        parser.error("--min-delay-ms must not exceed --max-delay-ms")

    return WorkerConfig(
        rabbitmq_url=args.rabbitmq_url,
        fail_rate=args.fail_rate,
        min_delay_ms=args.min_delay_ms,
        max_delay_ms=args.max_delay_ms,
        duplicate_rate=args.duplicate_rate,
        reorder=args.reorder,
        seed=args.seed,
        concurrency=args.concurrency,
        log_level=args.log_level,
    )


def _rate(value: str) -> float:
    rate = float(value)
    if not 0.0 <= rate <= 1.0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {value}")
    return rate


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return number


def _flag(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {value}")
