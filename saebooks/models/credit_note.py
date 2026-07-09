"""Credit note + lines.

Issued by the seller to the customer when goods/services are returned
or an invoice needs to be partially reversed. In GL terms, it is the
mirror of an invoice: debits income + GST Collected (giving the money
"back" to the customer), credits AR control.

Credits are then allocated against open invoices (reducing AR owed) or
refunded via a cash payment (``Payment.direction=OUTGOING`` with the
credit note's id in the allocation).
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
from saebooks.models._scope import CompanyScoped

if TYPE_CHECKING:
    # Relationship target types referenced only in stringized
    # (PEP 563) Mapped[...] annotations; import under TYPE_CHECKING
    # to satisfy static analysis without a runtime import cycle.
    from saebooks.models.one_off_customer import OneOffCustomer

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class CreditNoteStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class CreditNote(CompanyScoped, Base):
    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "number", name="uq_credit_notes_company_number"
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
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    one_off_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("one_off_customers.id", ondelete="RESTRICT"),
    )
    number: Mapped[str | None] = mapped_column(String(32))
    issue_date: Mapped[date]
    status: Mapped[CreditNoteStatus] = mapped_column(
        Enum(CreditNoteStatus, name="credit_note_status_enum"),
        nullable=False,
        default=CreditNoteStatus.DRAFT,
    )
    original_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
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
    amount_allocated: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Free-text payment terms (0171) — mirrors invoices.payment_terms.
    # Defaulted from Company.default_payment_terms at CREATE when the
    # payload doesn't supply a value; rendered on the credit-note PDF.
    payment_terms: Mapped[str | None] = mapped_column(Text)
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
    # --- Optimistic locking + multi-tenant (added cycle 10) ---------------
    # ``version`` starts at 1 on create; every PATCH via the API bumps it.
    # ``tenant_id`` defaults to the single default tenant so the legacy
    # service layer still works without change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=_DEFAULT_TENANT_ID,
    )

    lines: Mapped[list[CreditNoteLine]] = relationship(
        back_populates="credit_note",
        cascade="all, delete-orphan",
        order_by="CreditNoteLine.line_no",
    )
    one_off_customer: Mapped[OneOffCustomer | None] = relationship(
        "OneOffCustomer",
        foreign_keys=[one_off_customer_id],
        lazy="raise",
    )

    @property
    def one_off_customer_name(self) -> str | None:
        oc = self.__dict__.get("one_off_customer")
        return oc.name if oc is not None else None


class CreditNoteLine(Base):
    __tablename__ = "credit_note_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("credit_notes.id", ondelete="CASCADE"),
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

    credit_note: Mapped[CreditNote] = relationship(back_populates="lines")
