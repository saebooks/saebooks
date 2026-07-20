"""Generic money-in receipt + lines.

The engine's "money-in / negative-expense" record for money received that is
NOT tied to a customer invoice or a bill: supplier refunds, cashbacks, rebates,
an ATO GST refund, an insurance recovery (RCV-*), interest received, etc.

A receipt debits a bank/asset account and credits one or more income OR expense
accounts, with an optional GST line per line. Posting (see
``services/receipts.py``):

    Dr Bank/Asset ........................... total
    Cr Income / Cr Expense .................. amount (per line)
    Cr GST Collected (for income lines) ..... tax (reverse: a sale's GST)
    Cr GST Paid     (for expense lines) ..... tax (reverse input credit)

GST lines are built EXPLICITLY (never via ``gst_amount`` auto-posting) so the
sign is deterministic per line account type — the auto-poster would debit GST
Paid for an expense line, which is the wrong direction for a refund/negative
expense.

Schema materialised by migration ``0157_moneyin_and_review_flag``.

RLS (Class A — direct ``tenant_id`` column): migration 0157 applies
ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation`` policy and
the 0131 tenant<->company coherence trigger. ``bank_account_id`` is composite-
FK'd to ``accounts(id, company_id)`` so a receipt can never bank into a sister
company's account.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
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


class ReceiptStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class Receipt(CompanyScoped, Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "number", name="uq_receipts_company_number"
        ),
        # bank_account_id must be an account OF company_id (composite FK to the
        # 0152 uq_accounts_id_company target) so a receipt can't bank into a
        # sister company's account.
        ForeignKeyConstraint(
            ["bank_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_receipts_bank_account_company",
            ondelete="RESTRICT",
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
    # Destination bank/asset account, debited on post. Composite-FK'd above.
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
    )
    number: Mapped[str | None] = mapped_column(String(32))
    receipt_date: Mapped[date]
    status: Mapped[ReceiptStatus] = mapped_column(
        String(16),
        nullable=False,
        default=ReceiptStatus.DRAFT,
    )
    reference: Mapped[str | None] = mapped_column(String(255))
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

    lines: Mapped[list[ReceiptLine]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        order_by="ReceiptLine.line_no",
    )


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Income OR expense account credited on post.
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_codes.id", ondelete="SET NULL"),
    )
    # ``amount`` is the ex-GST (net) amount credited to ``account_id``;
    # ``tax_amount`` is the GST on it; ``line_total`` = amount + tax.
    amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )

    receipt: Mapped[Receipt] = relationship(back_populates="lines")
