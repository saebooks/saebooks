"""Idempotency-key table — Phase 0 API scaffolding.

Clients send ``X-Idempotency-Key: <uuid>`` on every write. The API
stores the key + response the first time it's seen; replays within
the 7-day retention window return the cached response verbatim.

The 7-day retention sweep isn't implemented here — a background job
in Phase 1 cycle 4 will delete rows older than 7 days.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
