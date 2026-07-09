"""Supplier rules — deterministic vendor → coding suggestions (issue #33 phase 2).

A ``SupplierRule`` maps a normalised vendor key (and optionally an
11-digit Australian Business Number) to a contact plus default account /
tax-code / record-kind. Matching runs at extraction time — ABN-exact
first, then vendor_key-exact, first match wins — and is **suggestion-
only** (the bank-rules posture): it fills ``InboxDocument.suggested_*``
and never publishes anything. No machine learning.

Conventions (mirrors ``models/inbox_document.py``):

* ``origin`` / ``record_kind`` are ``enum.StrEnum`` in Python, TEXT +
  CHECK in Postgres (migration 0175) — never a Postgres enum.
* Uniqueness is the hand-written partial expression index
  ``uq_supplier_rules_scope_vendor`` — one *active* rule per vendor per
  (tenant, company) scope, NULL company (tenant-wide) folded onto the
  nil UUID. Soft-delete via ``active=false`` frees the slot.
* ``times_applied`` / ``times_overridden`` are the rule-quality signal,
  maintained at publish time (confirmed application vs diverging
  publish).

Isolation is tenant-level: RLS (ENABLE + FORCE + ``tenant_isolation``
policy, migration 0175) plus explicit app-layer ``tenant_id`` filters on
every query. ``company_id`` is nullable by design (NULL = tenant-wide),
so the model is deliberately not ``CompanyScoped`` — same reasoning as
``InboxDocument``.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class SupplierRuleOrigin(enum.StrEnum):
    MANUAL = "MANUAL"
    LEARNED = "LEARNED"


class SupplierRule(Base):
    __tablename__ = "supplier_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        comment="NULL = tenant-wide rule; SET = scoped to one company",
    )
    vendor_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Normalised vendor name — services.document_inbox.normalise_vendor_key",
    )
    vendor_abn: Mapped[str | None] = mapped_column(
        String(11),
        comment="11-digit Australian Business Number, digits only",
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
    )
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_codes.id", ondelete="SET NULL"),
    )
    record_kind: Mapped[str | None] = mapped_column(
        Text, comment="Suggested publish kind: EXPENSE | BILL | CREDIT_NOTE"
    )
    origin: Mapped[SupplierRuleOrigin] = mapped_column(
        Text,
        nullable=False,
        default=SupplierRuleOrigin.MANUAL,
        server_default="MANUAL",
    )
    times_applied: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    times_overridden: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_from_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_documents.id", ondelete="SET NULL"),
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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
