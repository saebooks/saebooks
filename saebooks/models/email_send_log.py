"""ORM mapping for the ``email_send_log`` audit table.

Every attempted customer-facing email send (blocked or actual) writes an
immutable audit row here — captured attachment bytes/hashes so "what exact PDF
went out" is answerable from the row alone.

Historically the table was created only by Postgres raw-SQL migrations
(0123 create, 0124 attachment-bytes + Resend webhook columns, 0167 widened the
resend_status CHECK to include 'drafted'); no ORM model declared it. So
``saebooks.db.bootstrap_schema`` (SQLite/Community + the test harness) never
created it, and every email send on the Community/one-click edition failed to
write its audit row. This model mirrors the CUMULATIVE Postgres schema so
bootstrap_schema builds the table on SQLite and the ORM stays in lock-step with
the migration history (Postgres already has the table — no new migration).

The Postgres-only tamper triggers (migration 0125) and RLS policy (0123) are
NOT reproduced here: SQLite/Community is single-tenant by device, so there is no
RLS to enforce, and create_all cannot express the triggers. Column shapes match.

Defaults: create_all for this model runs on SQLite only (Postgres uses the
alembic migrations above), so the array/JSON columns take Python-side
``default=list`` rather than the Postgres literal server_defaults
(``'{}'::text[]`` / ``'[]'::jsonb``) which are not valid SQLite DDL. Only the
cross-dialect ``func.now()`` / integer-``0`` server_defaults are declared here.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class _SQLiteBytesArrayJSON(TypeDecorator):
    """SQLite variant for a ``BYTEA[]`` column: JSON list of base64 strings.

    Raw ``bytes`` are not JSON-serialisable, so a plain ``JSON()`` variant
    raises on the first send with a real attachment (the empty-list case hid
    this). Base64-encode on bind, decode on load, so callers see ``list[bytes]``
    on both backends.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: list[Any] | None, dialect: Any) -> list[Any] | None:
        if value is None:
            return None
        return [
            base64.b64encode(v).decode("ascii") if isinstance(v, (bytes, bytearray)) else v
            for v in value
        ]

    def process_result_value(self, value: list[Any] | None, dialect: Any) -> list[Any] | None:
        if value is None:
            return None
        return [base64.b64decode(v) if isinstance(v, str) else v for v in value]


class EmailSendLog(Base):
    __tablename__ = "email_send_log"
    __table_args__ = (
        CheckConstraint(
            "resend_status IN ('sent','failed','blocked','queued','drafted')",
            name="ck_email_send_log_status_valid",
        ),
        Index("ix_email_send_log_tenant_doc", "tenant_id", "doc_type", "doc_id"),
        Index("ix_email_send_log_tenant_sent_at", "tenant_id", "sent_at"),
        Index(
            "ix_email_send_log_resend_message_id",
            "resend_message_id",
            postgresql_where=text("resend_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    from_addr: Mapped[str] = mapped_column(Text, nullable=False)
    to_addrs: Mapped[list[str]] = mapped_column(ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False)
    cc_addrs: Mapped[list[str]] = mapped_column(
        ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    bcc_addrs: Mapped[list[str]] = mapped_column(
        ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_filenames: Mapped[list[str]] = mapped_column(
        ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    attachment_bytes: Mapped[list[bytes]] = mapped_column(
        ARRAY(BYTEA).with_variant(_SQLiteBytesArrayJSON(), "sqlite"),
        nullable=False,
        default=list,
    )
    attachment_sha256: Mapped[list[str]] = mapped_column(
        ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    attachment_content_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    resend_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    resend_status: Mapped[str] = mapped_column(String(16), nullable=False)
    resend_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_switch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Phase C (0124): Resend webhook delivery lifecycle columns.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bounced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bounce_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opened_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    clicked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    clicked_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    complained_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    webhook_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
