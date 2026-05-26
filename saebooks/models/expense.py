"""Expense model — paid-at-checkout supplier purchase.

Sibling of ``models/bill.py`` but with no AP-control step. Where a Bill
sits in Trade Creditors until a separate Payment closes it, an Expense
is paid in full at the moment of posting — the Cr goes straight to the
selected payment account (bank, card, or cash).

Posting journal shape (ex-GST line treatment):

    Dr Expense (per line) ......... line_subtotal
    Dr GST Paid ................... line_tax (auto-posted by gst.py)
    Cr Payment account (bank/card/cash) ... total

Key differences from ``Bill``:

* ``payment_account_id`` (NOT NULL FK ``accounts.id``) — the asset or
  liability account credited at post time. Bills credit AP (2-1200);
  Expenses credit whatever asset/liability paid at checkout.
* ``contact_id`` is NULLABLE — counter-shop receipts often have no
  known supplier on file.
* No ``due_date``, ``amount_paid``, retention split, or inventory
  receipt path. Expenses are simpler by design; if a workflow needs
  AP or stock receipt, raise a Bill or a PO+Bill instead.
* Number sequence is ``EX-`` (see ``services/numbering.py``).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class ExpenseStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class Expense(CompanyScoped, Base):
    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_expenses_company_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
    )
    one_off_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("one_off_vendors.id", ondelete="RESTRICT"),
    )
    payment_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    number: Mapped[str | None] = mapped_column(String(32))
    reference: Mapped[str | None] = mapped_column(String(64))
    expense_date: Mapped[date]
    status: Mapped[ExpenseStatus] = mapped_column(
        Enum(ExpenseStatus, name="expense_status_enum"),
        nullable=False,
        default=ExpenseStatus.DRAFT,
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AUD"
    )
    fx_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1")
    )
    base_subtotal: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    base_tax_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    base_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
    )
    void_journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_source: Mapped[str | None] = mapped_column(String(64))
    external_etag: Mapped[str | None] = mapped_column(String(255))
    external_payload: Mapped[dict | None] = mapped_column(JSONB)
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

    lines: Mapped[list[ExpenseLine]] = relationship(
        back_populates="expense",
        cascade="all, delete-orphan",
        order_by="ExpenseLine.line_no",
    )


class ExpenseLine(Base):
    __tablename__ = "expense_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    expense_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expenses.id", ondelete="CASCADE"),
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
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    line_tax: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    line_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )

    expense: Mapped[Expense] = relationship(back_populates="lines")
