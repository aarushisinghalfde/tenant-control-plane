"""Unit tests for the worker's flags and their environment-variable twins."""

import pytest

from worker.config import parse_config

URL = {"RABBITMQ_URL": "amqp://guest:guest@localhost:5672/"}


def test_defaults() -> None:
    config = parse_config([], URL)

    assert config.fail_rate == 0.0
    assert (config.min_delay_ms, config.max_delay_ms) == (500, 2000)
    assert config.duplicate_rate == 0.0
    assert config.reorder is False
    assert config.seed is None
    assert config.concurrency == 4


def test_environment_variables_set_values() -> None:
    env = URL | {
        "WORKER_FAIL_RATE": "1",
        "WORKER_MIN_DELAY_MS": "0",
        "WORKER_MAX_DELAY_MS": "10",
        "WORKER_DUPLICATE_RATE": "0.5",
        "WORKER_REORDER": "true",
        "WORKER_SEED": "42",
    }

    config = parse_config([], env)

    assert config.fail_rate == 1.0
    assert (config.min_delay_ms, config.max_delay_ms) == (0, 10)
    assert config.duplicate_rate == 0.5
    assert config.reorder is True
    assert config.seed == 42


def test_flags_win_over_environment_variables() -> None:
    env = URL | {"WORKER_FAIL_RATE": "1", "WORKER_REORDER": "true"}

    config = parse_config(["--fail-rate=0.25", "--reorder=false"], env)

    assert config.fail_rate == 0.25
    assert config.reorder is False


def test_bare_reorder_flag_means_true() -> None:
    assert parse_config(["--reorder"], URL).reorder is True


def test_empty_variable_counts_as_unset() -> None:
    config = parse_config([], URL | {"WORKER_SEED": "", "WORKER_FAIL_RATE": ""})

    assert config.seed is None
    assert config.fail_rate == 0.0


@pytest.mark.parametrize(
    "argv",
    [
        ["--fail-rate=1.5"],
        ["--fail-rate=-0.1"],
        ["--fail-rate=nan"],
        ["--duplicate-rate=2"],
        ["--min-delay-ms=-1"],
        ["--min-delay-ms=500", "--max-delay-ms=100"],
        ["--concurrency=0"],
        ["--reorder=maybe"],
    ],
)
def test_invalid_values_exit_with_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_config(argv, URL)

    assert exit_info.value.code == 2


def test_rabbitmq_url_is_required() -> None:
    with pytest.raises(SystemExit):
        parse_config([], {})
