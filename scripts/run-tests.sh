#!/usr/bin/env bash
# SAE Books — run isolated pytest stack and return its exit code.
#
# Usage: ./scripts/run-tests.sh [--rls] [extra pytest args...]
#
# --rls (or exporting SAEBOOKS_TEST_RLS=1) opts the runtime engine into
# the saebooks_app role (NOSUPERUSER + NOBYPASSRLS) instead of the
# owner/BYPASSRLS role, so FORCE ROW LEVEL SECURITY actually binds
# instead of being a no-op — see docker-compose.test.yml and
# docs/db-role-split.md. Without it (the default), behaviour is
# unchanged from before this flag existed: the suite runs under the
# owner role. Most test fixtures write directly via AsyncSessionLocal()
# without setting the tenant GUC, so --rls currently only runs green on
# the dedicated RLS/isolation test files that have been migrated to the
# tenant_session()/owner_seed_session() helpers in tests/conftest.py —
# running the whole suite with --rls is expected to be broadly red
# until the wider fixture migration lands.
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

# --rls opts the api service into the saebooks_app (NOBYPASSRLS) runtime
# engine — see docker-compose.test.yml. Strip it out of the positional
# args before they're forwarded to pytest as a target path. Honors
# SAEBOOKS_TEST_RLS=1 too, for callers that prefer an env var (e.g. CI
# matrix jobs) over a flag.
RLS_MODE=0
if [ "${SAEBOOKS_TEST_RLS:-0}" = "1" ]; then
    RLS_MODE=1
fi
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--rls" ]; then
        RLS_MODE=1
    else
        ARGS+=("$arg")
    fi
done

if [ "$RLS_MODE" = "1" ]; then
    echo "=== --rls: runtime engine uses saebooks_app (NOBYPASSRLS) ==="
    export SAEBOOKS_TEST_APP_DATABASE_URL="postgresql+asyncpg://saebooks_app:saebooks_app_test_pw@db:5432/saebooks_test"
else
    export SAEBOOKS_TEST_APP_DATABASE_URL=""
fi

# Pass any extra args through to pytest via PYTEST_TARGET (interpolated
# into the compose `command` at up time) so callers can scope a run to
# a subset like `./scripts/run-tests.sh -q tests/api/v1/test_invoices.py`
# WITHOUT the compose default `tests/` glob also being collected.
# Without this, PYTEST_ADDOPTS just appends args to the compose command,
# leaving `tests/` as a second positional and pytest unions both.
if [ "${#ARGS[@]}" -gt 0 ]; then
    export PYTEST_TARGET="${ARGS[*]}"
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
