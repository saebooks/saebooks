"""Append-only write log — Phase 0 API scaffolding.

Every write through saebooks.api.v1 appends a row here. Offline
desktop clients (Phase 4.5) pull changes since a known cursor to stay
in sync. Never mutated after insert; id is the cursor, ordered by
insertion.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class ChangeLog(Base):
    __tablename__ = "change_log"

    # BigInteger on Postgres (sequence-backed). On SQLite a ``BIGINT PRIMARY
    # KEY`` is NOT the special ``INTEGER PRIMARY KEY`` rowid alias, so it does
    # not autoincrement — every change_log insert (i.e. every write on the
    # SQLite/Community one-click) failed with "NOT NULL constraint failed:
    # change_log.id". The Integer variant emits ``INTEGER PRIMARY KEY`` on
    # SQLite (rowid-backed autoincrement) while leaving the Postgres DDL
    # unchanged.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=_DEFAULT_TENANT,
        comment="Owning tenant — set by the service layer before flush.",
    )
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    op: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
