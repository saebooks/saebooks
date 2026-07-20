"""Supplier (purchase) credit note + lines.

The purchase-side mirror of the customer ``credit_notes`` table. Issued by a
supplier to us when goods/services are returned or a bill needs to be partially
reversed (materials refund, rebate, cashback against a purchase). In GL terms it
is the mirror of a bill: it credits the expense and reverses the input GST,
debiting AP control (reducing what we owe the supplier) — i.e. the engine's
"money-in / negative-expense" record on the purchase side.

Posting (see ``services/supplier_credit_notes.py``):

    Dr AP Control (Trade Creditors 2-1200) .. total
    Cr Expense .............................. line_subtotal (per line)
    Cr GST Paid ............................. tax_total  (reverse input credit)

Schema materialised by migration ``0157_moneyin_and_review_flag``.

RLS (Class A — direct ``tenant_id`` column): migration 0157 applies
ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation`` policy and
the 0131 tenant<->company coherence trigger. The migration is the authoritative
DDL; the ORM does not add an RLS directive. The model is additionally
``CompanyScoped`` (app-layer company filter).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.db_types import Money
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class SupplierCreditNoteStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class SupplierCreditNote(CompanyScoped, Base):
    __tablename__ = "supplier_credit_notes"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "number",
            name="uq_supplier_credit_notes_company_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=_DEFAULT_TENANT_ID,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    number: Mapped[str | None] = mapped_column(String(32))
    issue_date: Mapped[date]
    status: Mapped[SupplierCreditNoteStatus] = mapped_column(
        String(16),
        nullable=False,
        default=SupplierCreditNoteStatus.DRAFT,
    )
    original_bill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bills.id", ondelete="SET NULL"),
    )
    supplier_reference: Mapped[str | None] = mapped_column(String(255))
    subtotal: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(String)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
    )
    void_journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list[SupplierCreditNoteLine]] = relationship(
        back_populates="supplier_credit_note",
        cascade="all, delete-orphan",
        order_by="SupplierCreditNoteLine.line_no",
    )


class SupplierCreditNoteLine(Base):
    __tablename__ = "supplier_credit_note_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    supplier_credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("supplier_credit_notes.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_codes.id", ondelete="SET NULL"),
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("1")
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    discount_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    line_subtotal: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    line_tax: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )

    supplier_credit_note: Mapped[SupplierCreditNote] = relationship(
        back_populates="lines"
    )
