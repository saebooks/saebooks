"""Document Inbox email-in — per-tenant addresses + message ledger.

``InboxEmailAddress`` is one server-minted ingestion address
``<token>@in.saebooks.com.au`` (spec issue #33 §4): the token is the
routing key AND the credential (12+ chars lowercase base32,
unguessable), globally unique across tenants via a plain unique
constraint (uniqueness must hold regardless of RLS visibility).
Multiple active addresses per tenant — one per company via
``company_id`` — because a multi-entity tenant wants per-company
routing. Revocation is a soft flip (``active=False`` + ``revoked_at``);
the row stays as the audit record.

``InboxEmailMessage`` is the per-message processing ledger, inserted
LAST in the poller walk (attachments first, ledger row last —
migration 0176 docstring has the crash-replay story).

Conventions: TEXT + CHECK in Postgres, never a Postgres enum;
RLS ENABLE + FORCE + ``tenant_isolation`` (migration 0176) plus explicit
app-layer ``tenant_id`` filters on every query.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class InboxEmailAddress(Base):
    __tablename__ = "inbox_email_addresses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        comment="Default company routing for documents from this address",
    )
    token: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        unique=True,
        comment="Routing key AND credential — lowercase base32, globally unique",
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class InboxEmailMessage(Base):
    __tablename__ = "inbox_email_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "mailbox", "message_id",
            name="uq_inbox_email_messages_msg",
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
    mailbox: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Catch-all mailbox identity (IMAP username / Graph mailbox UPN)",
    )
    message_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="RFC 5322 Message-ID"
    )
    from_addr: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    document_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
