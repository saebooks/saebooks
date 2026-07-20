"""Quote — pre-invoice sales document.

A Quote is a *commitment offer* from SAE to a customer. It has no GL
impact until it is ACCEPTED and then INVOICED (converted to an invoice).

Lifecycle
---------

    DRAFT  →  SENT  →  ACCEPTED  →  INVOICED   (terminal)
                    ↘  DECLINED                  (terminal)
                    (either state can go →)
                    →  ARCHIVED                  (terminal)

* DRAFT — editable, not yet sent. Default on create.
* SENT — delivered to the customer; awaiting response. Editable via
  versioned PATCH; changes after SENT should be resent.
* ACCEPTED — customer has accepted the terms. ``accepted_at`` stamped.
* DECLINED — customer declined. ``declined_at`` stamped. Terminal.
* ARCHIVED — manually archived (expired, superseded). Terminal.
* INVOICED — an invoice was raised from this quote. ``invoiced_at``
  stamped; ``invoice_id`` points to the created invoice. Terminal.

Quote terms
-----------

The canonical SAE Engineering terms (late fee 2.5%/month, 50% deposit,
28-day validity, supply-only handling) are NOT baked into the schema as
text — they live in the PDF render layer. The schema stores the numeric
parameters so they can vary per-quote and drive the render:

* ``validity_days`` — default 28. Expiry = issue_date + validity_days.
* ``deposit_pct`` — default 50. % of total required as deposit.
* ``late_fee_pct_per_month`` — default 2.5. Monthly rate on overdue
  invoices raised from this quote.
* ``is_supply_only`` — when True the PDF render adds the supply-only
  handling/fasteners clause.
* ``terms`` — free-text override of the standard terms block; NULL
  means "use the canonical template at render time."

Convert-to-invoice
------------------

``services.quotes.convert_to_invoice`` (future) will mint a DRAFT
``Invoice`` copying the quote's contact, lines, currency, and FX rate,
stamp ``invoiced_at`` / ``invoice_id`` on the quote, and flip its
status to INVOICED.

RLS
---

``quotes`` carries ``tenant_id`` directly so the standard
``tenant_isolation`` policy applies. ``quote_lines`` is scoped via
parent (mirrors ``invoice_lines`` — no policy on the child table).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
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

if TYPE_CHECKING:
    from saebooks.models.contact import Contact
    from saebooks.models.invoice import Invoice


class QuoteStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    ARCHIVED = "ARCHIVED"
    INVOICED = "INVOICED"


class Quote(CompanyScoped, Base):
    """Header record for a customer quote / estimate."""

    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_quotes_tenant_number"),
        Index(
            "ix_quotes_tenant_customer_status",
            "tenant_id",
            "customer_id",
            "status",
        ),
        Index(
            "ix_quotes_tenant_status_expiry",
            "tenant_id",
            "status",
            "expiry_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    number: Mapped[str | None] = mapped_column(String(32))
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[QuoteStatus] = mapped_column(
        Enum(QuoteStatus, name="quote_status_enum"),
        nullable=False,
        default=QuoteStatus.DRAFT,
    )
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)

    # Financials
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AUD"
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )

    # SAE canonical quote parameters (used at PDF render time)
    validity_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=28
    )
    deposit_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("50")
    )
    late_fee_pct_per_month: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("2.5")
    )
    is_supply_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Text fields
    title: Mapped[str | None] = mapped_column(String(255))
    scope: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)

    # Optimistic locking
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Lifecycle timestamps
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoiced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Back-reference to the invoice generated from this quote (INVOICED only)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
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

    lines: Mapped[list[QuoteLine]] = relationship(
        back_populates="quote",
        cascade="all, delete-orphan",
        order_by="QuoteLine.line_no",
    )
    customer: Mapped[Contact] = relationship(
        "Contact",
        foreign_keys=[customer_id],
        lazy="raise_on_sql",
    )
    invoice: Mapped[Invoice | None] = relationship(
        "Invoice",
        foreign_keys=[invoice_id],
        lazy="raise_on_sql",
    )


class QuoteLine(Base):
    """Line item on a quote."""

    __tablename__ = "quote_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quotes.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("1")
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_codes.id", ondelete="SET NULL"),
    )
    line_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    # Posting hint: which income account this line maps to when
    # convert-to-invoice runs. NULL is permitted at the schema level
    # so a salesperson can draft a quote before an accountant has
    # decided on GL coding — but ``saebooks.services.quotes`` rejects
    # convert-to-invoice on any quote that still has a NULL account_id
    # line (see services/quotes.py — "Cannot convert to invoice"),
    # which is the gate that protects the GL invariants.
    #
    # The audit (M3) suggested tightening this to NOT NULL. Decision
    # 2026-05-10: keep nullable. The service-layer gate is the right
    # place — a NOT NULL column would force a default account choice
    # at draft time, breaking the salesperson → accountant workflow,
    # without removing a real bug class (the convert path is already
    # gated and tested at tests/api/v1/test_quotes.py).
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
    )

    # Engineering-quote structured fields (mirrors the 6-col Overleaf table).
    # NULL on lines that don't need them (lump-sum items, subtotal rows).
    section_label: Mapped[str | None] = mapped_column(String(255))
    material: Mapped[str | None] = mapped_column(String(255))
    length_note: Mapped[str | None] = mapped_column(String(255))
    drawing_ref: Mapped[str | None] = mapped_column(String(255))

    quote: Mapped[Quote] = relationship(back_populates="lines")
