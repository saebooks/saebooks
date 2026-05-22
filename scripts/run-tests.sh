#!/usr/bin/env bash
# SAE Books — run isolated pytest stack and return its exit code.
#
# Usage: ./scripts/run-tests.sh [extra pytest args...]
#
# Any extra arguments are appended to the pytest command via the
# PYTEST_ADDOPTS env var so docker-compose.test.yml's command string
# does not need to be modified per-run.
#
# Project name "saebooks-test" ensures this stack is completely isolated
# from all live stacks (sauer / gecairns / app-preview / cashbook-demo).

set -euo pipefail

REPO_DIR="/home/sauer/projects/saebooks"
COMPOSE_FILE="$REPO_DIR/docker-compose.test.yml"
PROJECT="saebooks-test"

cd "$REPO_DIR"

# Pass any extra args through to pytest via PYTEST_ADDOPTS.
# e.g. ./scripts/run-tests.sh -k test_invoices -x
if [ $# -gt 0 ]; then
    export PYTEST_ADDOPTS="$*"
fi

echo "=== SAE Books test stack — $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
echo "=== Tearing down any stale test stack ==="
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans

echo "=== Building test image ==="
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" build

echo "=== Running test suite ==="
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" \
    up --abort-on-container-exit --exit-code-from api
exit_code=$?

echo "=== Tearing down test stack (exit $exit_code) ==="
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans

echo "=== Done — exit $exit_code ==="
exit "$exit_code"
