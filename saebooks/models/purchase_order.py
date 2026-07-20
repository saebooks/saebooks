"""Purchase Order — supplier procurement document.

A PO is a *commitment* document, not a financial event. It tells the
supplier what we're ordering and at what price; nothing posts to the GL
when a PO is created or sent. The financial event is the supplier's
bill (which the PO can be converted to in one step).

Lifecycle
---------

    DRAFT  →  OPEN  →  RECEIVED      ┐
                  ↘   PARTIAL ─→     │→  CLOSED
                  ↘   CANCELLED      ┘

* DRAFT — editable, not yet sent. Default on create.
* OPEN — sent to supplier, awaiting goods/services. Editable behind a
  versioned PATCH; lines and totals can still change but the audit log
  notes every revision.
* PARTIAL — at least one line has been received and converted to a
  bill, but not all of them. We track received_qty per line so a PO
  can be drained over multiple deliveries.
* RECEIVED — all lines fully received (received_qty == quantity for
  every line). One more click closes the PO.
* CLOSED — terminal. No further bills will be raised against this PO.
* CANCELLED — terminal. PO was sent, then withdrawn. Distinct from
  CLOSED so reports can tell "completed" from "abandoned".

Convert-to-bill
---------------

``services.purchase_orders.convert_to_bill`` mints a DRAFT ``Bill``
copying the PO's contact, lines (each line's ``quantity`` becomes
``quantity_remaining = quantity - received_qty``, never negative), tax
codes, currency and FX rate. The bill carries a back-reference to the
PO via ``Bill.purchase_order_id`` so reports can roll up
"committed-but-not-billed" exposure.

Multi-receipt is supported: a PO with 100 widgets can be billed in
batches of 30, 30, 40 over three converts. Each convert advances the
``received_qty`` on each line by the amount billed, and the PO status
auto-flips to PARTIAL after the first receipt and RECEIVED after the
last.

GL impact
---------

None at the PO layer. The bill is what posts. We deliberately keep
PO out of the GL — adding a "commitments" account is feature-flagged
v2 work and would invite double-counting if not done with care.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.db_types import Money
from saebooks.models._scope import CompanyScoped


class PurchaseOrderStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    RECEIVED = "RECEIVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class PurchaseOrder(CompanyScoped, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "number", name="uq_purchase_orders_company_number"
        ),
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
    expected_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus, name="purchase_order_status_enum"),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT,
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
    # Foreign-currency mirror of Bill — POs can be denominated in any
    # currency; ``fx_rate`` stamps the rate at issue, ``base_*`` carries
    # the base-currency view used for committed-spend reports.
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AUD"
    )
    fx_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1")
    )
    base_subtotal: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    base_tax_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    base_total: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    delivery_address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    external_id: Mapped[str | None] = mapped_column(String(255))
    external_source: Mapped[str | None] = mapped_column(String(64))
    external_etag: Mapped[str | None] = mapped_column(String(255))
    external_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

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

    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        order_by="PurchaseOrderLine.line_no",
    )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
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
    # Quantity already received via converted bills. Convert-to-bill
    # advances this; ``quantity - received_qty`` is what's still
    # outstanding on the PO.
    received_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
    )

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
