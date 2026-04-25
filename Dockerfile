# SAE Books API — multi-stage production Dockerfile
#
# Multi-arch: linux/amd64 and linux/arm64 are Tier-1 published binaries.
# linux/riscv64 is Tier-2 (best-effort, no SLA); it is known-buildable via
# QEMU on the saebooks buildx builder but takes 3-5× longer per build due to
# QEMU overhead. To include riscv64 add it to --platform on the buildx call.
#
# Build args
# ----------
# TARGETPLATFORM — injected by buildx; used here only for documentation.
# No arch-specific assembly or SIMD intrinsics are used in any dependency.
# All native deps (grpcio, asyncpg, cryptography, pydantic-core) build from
# source on any supported arch — this is enforced by the CHARTER rule at §5.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: builder — compile extension modules + install all deps into a venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ARG TARGETPLATFORM

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Build deps:
#   gcc, g++   — required by grpcio (C++ extension), cryptography (Rust + C shims)
#   libpq-dev  — asyncpg links against libpq at build time
#   curl       — used only in healthcheck in runtime stage; installed there separately
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc g++ libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"

WORKDIR /build

# Copy dependency manifest first (layer-cache friendly — only reinstalls
# when pyproject.toml changes, not on every source edit).
COPY pyproject.toml README.md ./

# Install runtime deps only (no [dev] extras in production image).
# pip resolves from pyproject.toml; no separate requirements.txt needed.
RUN pip install --upgrade pip setuptools wheel \
    && pip install .

# Copy application source after deps so cache is only busted by source changes.
COPY saebooks/ ./saebooks/
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Compile gRPC stubs. grpcio-tools is a declared runtime dep so protoc is
# available in the venv. The grpc_gen directory is gitignored (generated code)
# so we produce it here rather than relying on a checked-in copy.
RUN mkdir -p saebooks/grpc_gen \
    && touch saebooks/grpc_gen/__init__.py \
    && python -m grpc_tools.protoc \
        -I saebooks/proto \
        --python_out=saebooks/grpc_gen \
        --grpc_python_out=saebooks/grpc_gen \
        saebooks/proto/saebooks.proto \
    && sed -i 's/^import saebooks_pb2/from saebooks.grpc_gen import saebooks_pb2/' \
        saebooks/grpc_gen/saebooks_pb2_grpc.py

# Re-install in editable-equivalent mode so package metadata is registered.
# We copy the egg-info directory to the venv so importlib.metadata can find
# the package version at runtime.
RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal image, no compiler toolchain
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Runtime deps:
#   libpq5  — asyncpg dynamically links against libpq at runtime
#   curl    — required by HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — principle of least privilege.
RUN groupadd --system saebooks \
    && useradd --system --gid saebooks --no-create-home saebooks

WORKDIR /app

# Copy venv from builder (includes all compiled extensions + package metadata).
COPY --from=builder /opt/venv /opt/venv

# Copy application source. We don't mount volumes in production —
# the entire app is baked in.
COPY --chown=saebooks:saebooks saebooks/ ./saebooks/
# Bring in the generated gRPC stubs from the builder stage.
COPY --from=builder --chown=saebooks:saebooks /build/saebooks/grpc_gen/ ./saebooks/grpc_gen/
COPY --chown=saebooks:saebooks alembic.ini ./
COPY --chown=saebooks:saebooks alembic/ ./alembic/
COPY --chown=saebooks:saebooks entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

USER saebooks

EXPOSE 8000

# Healthcheck hits /api/v1/healthz (unauthenticated, no DB round-trip).
# --start-period allows time for Alembic migrations + import warm-up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]
