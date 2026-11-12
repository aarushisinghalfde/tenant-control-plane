#!/bin/sh
# Quality gate. Every step must pass; the first failure stops the run.
# Run it with:  docker compose --profile tools run --rm --build check
set -eu

step() { printf '\n==> %s\n' "$1"; }

step "Format (ruff format --check)"
ruff format --check .

step "Lint (ruff check)"
ruff check .

step "Types (mypy --strict)"
mypy

step "Security: code (bandit)"
bandit -q -c pyproject.toml -r app worker

step "Security: dependencies (pip-audit)"
pip-audit

printf '\nAll checks passed.\n'
