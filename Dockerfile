FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY saebooks/ ./saebooks/
COPY alembic.ini ./
COPY alembic/ ./alembic/

RUN pip install -e ".[dev]"

EXPOSE 8000

# Healthcheck hits the /health endpoint. --start-period gives the app
# room to run migrations + warm imports before the first probe counts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "saebooks.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
