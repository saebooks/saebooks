"""Audit snapshots — row-level state capture before risky edits.

When a protected or system-managed account is modified, the pre-edit
state is saved here. This enables "undo" without needing full DB
backups, and provides an audit trail for compliance.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class AuditSnapshot(Base):
    __tablename__ = "audit_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    row_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Primary key of the snapshotted row (UUID as string)",
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="What triggered the snapshot: update, delete, migrate",
    )
    before_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False,
        comment="Full row state before the change",
    )
    after_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Full row state after the change (null for deletes)",
    )
    reason: Mapped[str | None] = mapped_column(Text)
    performed_by: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
