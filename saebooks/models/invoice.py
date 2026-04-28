"""AR invoice model.

Two tables: ``invoices`` and ``invoice_lines``. Lifecycle:

    DRAFT  -->  POSTED  -->  VOIDED

A DRAFT is editable and has no GL impact. Posting generates the
invoice number (via ``services/numbering.py``), debits the AR control
account, credits income + GST Collected (handled by the existing GST
auto-post in ``services/journal.py``), and stamps ``journal_entry_id``
+ ``posted_at``. Voiding reverses the posting journal and stamps
``void_journal_entry_id``; nothing is hard-deleted so the audit trail
survives.

Line-level tax treatment is add-on (ex-GST) for v1 — ``line_subtotal =
qty * unit_price * (1 - discount_pct/100)`` and ``line_tax =
line_subtotal * rate``. Tax-inclusive treatment will come as a
per-invoice flag in a later batch.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class InvoiceStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class Invoice(CompanyScoped, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_invoices_company_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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
    due_date: Mapped[date]
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status_enum"),
        nullable=False,
        default=InvoiceStatus.DRAFT,
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
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    # --- Foreign-currency header (Batch GG/2) ------------------------------
    # ``currency`` is the document currency (ISO 4217). ``fx_rate`` is
    # the rate from document currency → company base currency at issue
    # time. Both default so AUD-only installs never have to think about
    # FX. The ``base_*`` shadow columns carry the base-currency view of
    # the totals — GL postings use those so the book of account stays
    # in the base currency regardless of the issue currency.
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
    base_amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    # Real-estate commission timing (RLES-6). When set, the GL posting
    # uses this date as entry_date instead of issue_date so BAS period
    # attribution reflects unconditional exchange or settlement rather
    # than the invoice issue date.
    settlement_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # --- Optimistic locking + multi-tenant (added cycle 7) ----------------
    # ``version`` starts at 1 on create; every PATCH via the API bumps it.
    # ``tenant_id`` defaults to the single default tenant so the legacy
    # service layer (create_draft / post_invoice) still works without change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Stripe payment link (B/48) ----------------------------------------
    # URL of the Stripe Checkout Session generated for this invoice.  Null
    # until a payment link is created via POST /api/v1/invoices/{id}/stripe-payment-link.
    # Gated by FLAG_STRIPE_INTEGRATION at the router level; stored here so
    # the URL survives across requests and can be included in email templates.
    stripe_payment_link: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.line_no",
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
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
    # Optional project tag for P&L-by-project / job-costing reports.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    # Optional inventory item — when set, posting this line issues
    # stock: Dr COGS / Cr Inventory at WAC (in addition to the normal
    # Dr AR / Cr Income). ``account_id`` is overridden to the item's
    # ``income_account_id`` at ``_replace_lines`` time so GL stays
    # consistent with the stock movement. SET NULL on delete mirrors
    # the project_id FK.
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
    )
    # Franking credit annotation (PRTR-4). When a line represents dividend
    # income, these fields carry the imputation credit and percentage so
    # grossed-up income can be calculated without a separate GL account.
    # franking_percentage is the % of the dividend that is franked (0-100);
    # franking_credit_amount is the absolute dollar value of the tax offset.
    franking_credit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    franking_percentage: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    # Deferred-revenue support (FITC-3). When set, posting routes this line's
    # income portion to Unearned Income (2-1760) rather than the income
    # account directly. Monthly recognition JEs are generated by
    # services/deferred_revenue.py. recognized_through_date tracks the
    # last period (first-of-month) for which revenue has been recognized.
    service_start_date: Mapped[date | None] = mapped_column(Date)
    service_end_date: Mapped[date | None] = mapped_column(Date)
    recognized_through_date: Mapped[date | None] = mapped_column(Date)
    # Margin-scheme acquisition cost (MOTR-1 / Div 75 s66-50). When the
    # line's tax code has reporting_type="margin_scheme", the GST is
    # 1/11 × (line_subtotal − margin_acq_cost) rather than rate % of
    # subtotal. NULL for all non-margin-scheme lines.
    margin_acq_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    # Retention percentage (CIVL-2). Civil/construction progress claims
    # often withhold a percentage (e.g. 5%) pending practical completion.
    # At posting, the Dr AR side is split: Trade Debtors receives
    # line_subtotal × (1 - retention_pct/100) + full GST, while
    # Retentions Receivable (1-1220) receives line_subtotal × retention_pct/100.
    # Revenue and GST are recognised on the full claim amount — not reduced.
    retention_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    # Trade-in flag (MOTR-2). When True this line represents a vehicle
    # acquired via trade-in. It is excluded from the invoice GL posting
    # (so G1 shows the full new-car sale price, not the net settlement),
    # and post_invoice auto-creates a companion AP bill
    # (Dr Inventory / Cr Trade Creditors) with independent GST treatment.
    is_trade_in: Mapped[bool] = mapped_column(
        nullable=False, default=False
    )

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
