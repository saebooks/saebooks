"""Recurring invoice template.

A ``RecurringInvoice`` is an invoice pattern + schedule: it holds
everything needed to fork a fresh DRAFT ``Invoice`` on a cadence
(weekly/fortnightly/monthly/quarterly/yearly). The CLI entry
``python -m saebooks.cli generate-recurring`` (kicked by cron) asks
the service which templates are due and materialises them one by one.

Month-end safety:
    When ``frequency`` is MONTHLY / QUARTERLY / YEARLY, the next run
    date is computed by "snap to anchor day, cap at month-end" —
    e.g. a 31-Jan MONTHLY rolls to 28-Feb (or 29-Feb in a leap year)
    and then back up to 31-Mar. Storing ``anchor_day`` keeps that
    round-trip from drifting down to the 28th.

``auto_post``:
    When ``True``, the CLI runs ``services/invoices.post_invoice``
    immediately after minting the draft. Default ``False`` so
    humans get a chance to review before the GL entry lands.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class RecurrenceFrequency(enum.StrEnum):
    WEEKLY = "WEEKLY"
    FORTNIGHTLY = "FORTNIGHTLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class RecurrenceStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class RecurringInvoice(CompanyScoped, Base):
    __tablename__ = "recurring_invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    frequency: Mapped[RecurrenceFrequency] = mapped_column(
        Enum(RecurrenceFrequency, name="recurrence_frequency_enum"),
        nullable=False,
    )
    status: Mapped[RecurrenceStatus] = mapped_column(
        Enum(RecurrenceStatus, name="recurrence_status_enum"),
        nullable=False,
        default=RecurrenceStatus.ACTIVE,
    )
    anchor_day: Mapped[int | None] = mapped_column(Integer)
    next_run: Mapped[date] = mapped_column(nullable=False)
    end_date: Mapped[date | None]
    last_run: Mapped[date | None]
    due_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    auto_post: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    invoices_generated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
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

    lines: Mapped[list[RecurringInvoiceLine]] = relationship(
        back_populates="recurring_invoice",
        cascade="all, delete-orphan",
        order_by="RecurringInvoiceLine.line_no",
    )


class RecurringInvoiceLine(Base):
    __tablename__ = "recurring_invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recurring_invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recurring_invoices.id", ondelete="CASCADE"),
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

    recurring_invoice: Mapped[RecurringInvoice] = relationship(
        back_populates="lines"
    )
