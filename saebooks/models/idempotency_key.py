"""Idempotency storage models.

Two tables coexist here:

``idempotency_keys`` (Phase 0 — legacy)
    UUID primary key, no body hash, no tenant scoping.  Used by the
    per-router ``_idempotent_replay`` / ``_remember_idempotent`` helpers
    that cannot be changed in this sprint.  Race-unsafe; retained for
    backward compatibility only.

``idempotency_records`` (migration 0057 — race-safe)
    TEXT primary key (the raw header value), per-tenant scoping,
    SHA-256 body hash for RFC 8417 mismatch detection, and a BYTEA
    response body.  Written via the race-safe service at
    ``saebooks.services.idempotency`` using ``INSERT … ON CONFLICT
    DO UPDATE … RETURNING *`` so concurrent writers serialise correctly.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, LargeBinary, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base

# ---------------------------------------------------------------------------
# Legacy table (Phase 0) — do not change; router files depend on it
# ---------------------------------------------------------------------------


class IdempotencyKey(Base):
    """Phase 0 idempotency table.  Race-unsafe; use IdempotencyRecord instead."""

    __tablename__ = "idempotency_keys"

    key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# Race-safe table (migration 0057)
# ---------------------------------------------------------------------------


class IdempotencyRecord(Base):
    """Race-safe idempotency record (migration 0057).

    Written via ``INSERT … ON CONFLICT DO UPDATE … RETURNING *`` so
    concurrent writers always get a deterministic winner without racing
    on an application-level SELECT + INSERT.

    The PRIMARY KEY on ``idempotency_key`` provides the UNIQUE
    constraint that ON CONFLICT targets.
    """

    __tablename__ = "idempotency_records"

    # Raw header value — TEXT so any valid UUID (with or without hyphens)
    # or custom client-generated key is stored verbatim.
    idempotency_key: Mapped[str] = mapped_column(Text(), primary_key=True)
    # Tenant scoping — a key from tenant A must never replay for tenant B.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # SHA-256 hex digest of the raw request body.  RFC 8417 §2.1 requires
    # a 422 when the same key is replayed with a different body.
    body_sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    # HTTP status code stored so replays can return the original status.
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    # Serialised JSON response body (UTF-8 bytes).  BYTEA avoids any
    # JSON re-encoding round-trip that could alter floating-point values.
    response_body: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
