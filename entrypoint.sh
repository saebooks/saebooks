#!/bin/bash
set -e
alembic upgrade head
if [ "${SAEBOOKS_RUN_SEED:-false}" = "true" ]; then
    python -m saebooks.cli.seed_dev
fi
exec uvicorn saebooks.main:app --host 0.0.0.0 --port 8000
