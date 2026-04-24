#!/bin/bash
set -e
alembic upgrade head
exec uvicorn saebooks.main:app --host 0.0.0.0 --port 8000
