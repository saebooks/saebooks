"""Payment + PaymentAllocation.

A ``Payment`` represents cash moving in either direction:

* ``INCOMING`` — receipt from a customer. Debits Bank, credits AR
  control (``1-1200 Trade Debtors``). Batch S ships this path.
* ``OUTGOING`` — payment to a supplier. Debits AP control
  (``2-1200 Trade Creditors``) + the Bank bank account. Model is in
  place; service wiring lands in Batch V (Bills) so there's an AP row
  to debit against.

Allocations link a payment to one or more invoices (customer receipts)
or credit notes (supplier refund). An unallocated payment is valid —
it's a "receipt on account". Allocation is a separate concern from
posting: post the GL journal at payment time; allocate whenever the
user has matched it.
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


class PaymentStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class PaymentDirection(enum.StrEnum):
    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"


class PaymentMethod(enum.StrEnum):
    CASH = "cash"
    EFT = "eft"
    CHEQUE = "cheque"
    CARD = "card"
    DIRECT_DEPOSIT = "direct_deposit"
    OTHER = "other"


class Payment(CompanyScoped, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_payments_company_number"),
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
    one_off_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("one_off_customers.id", ondelete="RESTRICT"),
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    number: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[PaymentDirection] = mapped_column(
        Enum(PaymentDirection, name="payment_direction_enum"), nullable=False
    )
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method_enum",
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
        default=PaymentMethod.EFT,
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status_enum"),
        nullable=False,
        default=PaymentStatus.DRAFT,
    )
    payment_date: Mapped[date]
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    # --- Foreign-currency header (Batch GG/2) ------------------------------
    # The payment may settle a foreign-currency invoice/bill at a
    # *different* rate than the one stamped on the document — that rate
    # difference is the realised FX gain/loss posted by
    # ``services/fx/settle.py`` during allocation.
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AUD"
    )
    fx_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1")
    )
    base_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    reference: Mapped[str | None] = mapped_column(String(128))
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
    # --- Optimistic locking + multi-tenant (added cycle 9) ----------------
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

    allocations: Mapped[list[PaymentAllocation]] = relationship(
        back_populates="payment",
        cascade="all, delete-orphan",
    )
    one_off_vendor: Mapped["OneOffVendor | None"] = relationship(
        "OneOffVendor",
        foreign_keys=[one_off_vendor_id],
        lazy="raise",
    )
    one_off_customer: Mapped["OneOffCustomer | None"] = relationship(
        "OneOffCustomer",
        foreign_keys=[one_off_customer_id],
        lazy="raise",
    )

    @property
    def one_off_vendor_name(self) -> str | None:
        ov = self.__dict__.get("one_off_vendor")
        return ov.name if ov is not None else None

    @property
    def one_off_customer_name(self) -> str | None:
        oc = self.__dict__.get("one_off_customer")
        return oc.name if oc is not None else None


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
    )
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("credit_notes.id", ondelete="RESTRICT"),
    )
    bill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bills.id", ondelete="RESTRICT"),
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    payment: Mapped[Payment] = relationship(back_populates="allocations")
