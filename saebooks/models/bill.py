"""AP bill model — supplier invoice.

Mirror of ``models/invoice.py`` with the signs flipped on post:

    Dr Expense (per line) ........ line_subtotal
    Dr GST Paid .................. line_tax (auto-posted by gst.py)
    Cr Trade Creditors (AP) ...... total

Two small differences from the AR side:

* ``supplier_reference`` — the supplier's own invoice number. We carry
  it so that the remittance advice can quote it back.
* No ``sent_at`` / ``payment_terms`` column set — the supplier is the
  one who already sent it; terms belong on their end.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

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
from saebooks.db_types import Money
from saebooks.models._scope import CompanyScoped

if TYPE_CHECKING:
    # Relationship target types referenced only in stringized
    # (PEP 563) Mapped[...] annotations; import under TYPE_CHECKING
    # to satisfy static analysis without a runtime import cycle.
    from saebooks.models.one_off_vendor import OneOffVendor


class BillStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class Bill(CompanyScoped, Base):
    __tablename__ = "bills"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_bills_company_number"),
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
        nullable=False,
    )
    one_off_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("one_off_vendors.id", ondelete="RESTRICT"),
    )
    number: Mapped[str | None] = mapped_column(String(32))
    supplier_reference: Mapped[str | None] = mapped_column(String(64))
    issue_date: Mapped[date]
    due_date: Mapped[date]
    status: Mapped[BillStatus] = mapped_column(
        Enum(BillStatus, name="bill_status_enum"),
        nullable=False,
        default=BillStatus.DRAFT,
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
    amount_paid: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    # --- Foreign-currency header (Batch GG/2) ------------------------------
    # Mirror of ``Invoice`` — the supplier's document may be in any
    # currency; we store the rate to base at bill-issue date and the
    # base-currency totals for GL posting.
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
    base_amount_paid: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(String)
    # --- Optimistic locking + multi-tenant (added cycle 8) ----------------
    # ``version`` starts at 1 on create; every PATCH via the API bumps it.
    # ``tenant_id`` defaults to the single default tenant so the legacy
    # service layer still works without change.
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
    external_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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

    lines: Mapped[list[BillLine]] = relationship(
        back_populates="bill",
        cascade="all, delete-orphan",
        order_by="BillLine.line_no",
    )
    one_off_vendor: Mapped[OneOffVendor | None] = relationship(
        "OneOffVendor",
        foreign_keys=[one_off_vendor_id],
        lazy="raise",
    )

    @property
    def one_off_vendor_name(self) -> str | None:
        ov = self.__dict__.get("one_off_vendor")
        return ov.name if ov is not None else None


class BillLine(Base):
    __tablename__ = "bill_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bills.id", ondelete="CASCADE"),
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
    # Optional project tag for cost-by-project / job-costing reports.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    # Optional inventory item — when set, posting this line receives
    # stock: Dr Inventory (at line_base_subtotal/qty unit cost) updates
    # WAC. ``account_id`` is overridden to the item's
    # ``inventory_account_id`` at ``_replace_lines`` time so the GL
    # inventory balance stays consistent with the stock movement.
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
    )
    # Civil construction retention hold (CIVL-3). Percentage of the
    # ex-GST line amount withheld from the immediate payable; splits
    # Cr Trade Creditors (net payable) + Cr Retentions Payable (held).
    # Zero means standard full-payment posting (no split).
    retention_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    # Motor-dealer floorplan tracking (MOTR-4). Optional VIN or internal
    # stock number that tags this cost line to a specific vehicle so that
    # per-unit gross margin can be computed net of floorplan interest.
    tracking_vehicle_id: Mapped[str | None] = mapped_column(String(64))

    bill: Mapped[Bill] = relationship(back_populates="lines")
