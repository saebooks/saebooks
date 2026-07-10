#!/usr/bin/env bash
# SAE Books — run isolated pytest stack and return its exit code.
#
# Usage: ./scripts/run-tests.sh [extra pytest args...]
#
# Any extra arguments are appended to the pytest command via the
# PYTEST_ADDOPTS env var so docker-compose.test.yml's command string
# does not need to be modified per-run.
#
# Project name defaults to "saebooks-test", but if SAEBOOKS_TEST_PROJECT
# is exported (e.g. by a worktree-specific wrapper) it overrides — that
# lets parallel fix-agent worktrees run tests concurrently without
# colliding on container/volume names. Worktree-resolution: the script
# resolves REPO_DIR from its own location, NOT a hardcoded path, so
# `worktree-X/scripts/run-tests.sh` uses `worktree-X/docker-compose.test.yml`.

set -euo pipefail

# Resolve to the worktree this script lives in (not a hardcoded /home path)
# so that fix-agent worktrees use their own compose file + source tree.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_DIR/docker-compose.test.yml"

# Default project name picks a per-worktree suffix from the repo path so
# saebooks-fix-A/B/C/... do not clash. Override with SAEBOOKS_TEST_PROJECT
# for fully-explicit naming (preserves the original "saebooks-test" name
# when running from /home/youruser/projects/saebooks itself).
if [ -n "${SAEBOOKS_TEST_PROJECT:-}" ]; then
    PROJECT="$SAEBOOKS_TEST_PROJECT"
elif [ "$(basename "$REPO_DIR")" = "saebooks" ]; then
    PROJECT="saebooks-test"
else
    # Derive a deterministic suffix from the worktree basename — keeps
    # it human-readable and stable across script invocations.
    PROJECT="saebooks-test-$(basename "$REPO_DIR" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^saebooks-//;s/^-*//;s/-*$//')"
fi

cd "$REPO_DIR"

# Pass any extra args through to pytest via PYTEST_TARGET (interpolated
# into the compose `command` at up time) so callers can scope a run to
# a subset like `./scripts/run-tests.sh -q tests/api/v1/test_invoices.py`
# WITHOUT the compose default `tests/` glob also being collected.
# Without this, PYTEST_ADDOPTS just appends args to the compose command,
# leaving `tests/` as a second positional and pytest unions both.
if [ $# -gt 0 ]; then
    export PYTEST_TARGET="$*"
fi

# Cleanup runs on Ctrl-C / SIGTERM / normal exit so we never leak
# containers and volumes belonging to THIS project. We avoid touching
# anything we did not start by scoping strictly to the project name.
cleanup() {
    local rc=$?
    echo "=== Tearing down test stack (rc=$rc, project=$PROJECT) ==="
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans 2>&1 || true
    exit "$rc"
}
trap cleanup EXIT INT TERM

echo "=== SAE Books test stack — $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
echo "=== REPO_DIR=$REPO_DIR ==="
echo "=== PROJECT=$PROJECT ==="
echo "=== Tearing down any stale test stack (this project only) ==="
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans

echo "=== Building test image ==="
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" build

echo "=== Running test suite ==="
# Disable the EXIT trap during the up call so its exit code is captured
# cleanly — re-arm afterwards so post-run teardown still happens.
set +e
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" \
    up --abort-on-container-exit --exit-code-from api
exit_code=$?
set -e

echo "=== Tests finished — exit $exit_code ==="
# Explicit teardown so the message order is clean; trap will be a no-op.
trap - EXIT INT TERM
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans

echo "=== Done — exit $exit_code ==="
exit "$exit_code"
