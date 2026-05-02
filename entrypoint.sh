#!/bin/bash
set -e
# One-shot mode: if any args were passed (e.g. \`docker run ... sh -c '...'\`),
# exec them directly without running alembic migrations or seeding.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
alembic upgrade head
if [ "${SAEBOOKS_RUN_SEED:-false}" = "true" ]; then
    python -m saebooks.cli.seed_dev
fi
exec uvicorn saebooks.main:app --host 0.0.0.0 --port 8000
