"""ORM models for supplier-statement reconciliation.

A ``SupplierStatement`` is a periodic statement of account received from a
supplier (typically via Paperless), parsed into ``SupplierStatementLine`` rows
and reconciled against the bills in our books. Schema materialised by migration
``0150_supplier_statements``.

Books-safety: these tables are a *review* surface only — reconciliation never
posts to the GL. Drafting a bill from a missing line / confirming is always an
explicit user action (Phase 3). See plan
``saebooks-statement-recon-productionisation``.

RLS (Class A — direct tenant_id column): migration 0150 applies
ENABLE + FORCE ROW LEVEL SECURITY + the ``tenant_isolation`` policy (the same
``app.current_tenant`` predicate as 0088/0055) to BOTH tables. The migration is
the authoritative DDL for production; the ORM does not add an RLS directive.
``SupplierStatement`` is additionally ``CompanyScoped`` (app-layer company
filter via ``services.tenant._scope_guard``).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Date,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class StatementStatus(enum.StrEnum):
    PENDING_EXTRACT = "pending_extract"   # row created, extraction not yet run
    EXTRACTED = "extracted"               # parsed; reconciliation attempted
    NEEDS_REVIEW = "needs_review"         # a gate flagged it (balance/extraction)
    RECONCILED = "reconciled"             # balance ties + no open exceptions
    DISMISSED = "dismissed"               # not an AP statement / user-dismissed


class StatementLineType(enum.StrEnum):
    INVOICE = "invoice"
    PAYMENT = "payment"
    CREDIT = "credit"
    ADJUSTMENT = "adjustment"
    UNKNOWN = "unknown"


class StatementMatchStatus(enum.StrEnum):
    MATCHED = "matched"
    AMOUNT_MISMATCH = "amount_mismatch"
    MISSING_IN_BOOKS = "missing_in_books"
    NOT_ON_STATEMENT = "not_on_statement"
    PAYMENT_INFO = "payment_info"
    SETTLED_NOT_IN_BOOKS = "settled_not_in_books"
    UNMATCHED = "unmatched"               # not yet reconciled


# Column lengths sized to the longest enum value + headroom.
_STATUS_LEN = 32
_LINETYPE_LEN = 16
_MATCH_LEN = 24


class SupplierStatement(CompanyScoped, Base):
    """A supplier statement of account, parsed for reconciliation."""

    __tablename__ = "supplier_statements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Resolved supplier (null until matched to a contact).
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Source Paperless document id (null for manual uploads).
    source_document_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # Extracted header fields (pre-resolution, as the supplier wrote them).
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_abn: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="AUD")

    # Reconciliation outputs.
    status: Mapped[str] = mapped_column(
        String(_STATUS_LEN),
        nullable=False,
        server_default=StatementStatus.PENDING_EXTRACT.value,
    )
    our_ap_as_at: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    balance_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Extraction provenance: model used, whether opus-escalated, anomalies, gate results.
    extraction_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    lines: Mapped[list[SupplierStatementLine]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        order_by="SupplierStatementLine.line_date",
    )


class SupplierStatementLine(Base):
    """One line of a supplier statement (invoice / payment / credit) and its
    reconciliation verdict against our books."""

    __tablename__ = "supplier_statement_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    statement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("supplier_statements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    line_type: Mapped[str] = mapped_column(
        String(_LINETYPE_LEN), nullable=False,
        server_default=StatementLineType.UNKNOWN.value,
    )
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    match_status: Mapped[str] = mapped_column(
        String(_MATCH_LEN), nullable=False,
        server_default=StatementMatchStatus.UNMATCHED.value,
    )
    matched_bill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bills.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    statement: Mapped[SupplierStatement] = relationship(back_populates="lines")
