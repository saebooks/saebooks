#!/bin/bash
set -e
# One-shot mode: if any args were passed (e.g. `docker run ... sh -c '...'`),
# exec them directly without running alembic migrations or seeding.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# --- migrate-on-boot preflight (fail-closed) ---------------------------------
# Guard the system-of-record against booting an image whose alembic history does
# NOT contain the database's current revision — i.e. an OLD / rollback / test
# image started against a DB that a NEWER image already migrated. Without this,
# the only symptom is an opaque crash-loop ("Can't locate revision XXXX"). Fail
# fast with a clear, actionable message instead — never silently downgrade
# (a downgrade on posted double-entry data is unsafe). A clean forward upgrade
# (DB at or behind this image's head, or a fresh empty DB) proceeds normally.
if ! alembic current >/tmp/.alembic_current.out 2>&1; then
    if grep -q "Can't locate revision" /tmp/.alembic_current.out; then
        echo "FATAL: alembic preflight — this image does not contain the database's current revision." >&2
        echo "       The DB was migrated by a NEWER image than this one (rollback/old/test image vs an already-upgraded DB)." >&2
        echo "       Refusing to start: deploy the matching-or-newer image, or restore the pre-deploy DB dump. Not downgrading." >&2
        sed 's/^/       alembic: /' /tmp/.alembic_current.out >&2
        exit 1
    fi
    # Any other failure (e.g. DB not ready yet) falls through to the upgrade
    # step below, which will surface it; compose depends_on waits for db health.
fi

alembic upgrade head
if [ "${SAEBOOKS_RUN_SEED:-false}" = "true" ]; then
    python -m saebooks.cli.seed_dev
fi
if [ "${SAEBOOKS_RUN_CASHBOOK_DEMO_SEED:-false}" = "true" ]; then
    python -m saebooks.cli.seed_cashbook_demo
fi
WORKERS="${SAEBOOKS_UVICORN_WORKERS:-1}"
exec uvicorn saebooks.main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS"
