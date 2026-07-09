"""Canonical bank-routing identifiers (M1.5 · T10).

Today bank routing is AU-only, scattered across fixed columns:
``Account.bsb`` / ``Account.apca_user_id``, ``Contact.bank_bsb``,
``Employee.bsb_encrypted`` and ``SuperFund.smsf_bsb_encrypted``. None
of that has anywhere to put an IBAN, a SWIFT/BIC, a US ABA routing
number, a UK sort code or a SEPA reference, so every non-AU company
either lies in the BSB field or goes without.

This table is a jurisdiction-neutral **superset**, keyed by
``(company_id, owner_type, owner_id, routing_scheme)`` so one owner
row can carry more than one routing identifier (e.g. a local BSB
*and* an IBAN for the same bank account). It does NOT replace the
legacy columns — those stay for backward compatibility and existing
callers keep working unchanged; new/updated code migrates onto this
table over time, the same additive posture as ``BusinessIdentifier``
generalising ``Company.abn``.

``owner_type`` + ``owner_id`` is a polymorphic reference (account,
contact, employee, or super_fund) rather than four nullable FK
columns, because exactly one of those tables owns any given row and
a single non-nullable pair keeps the schema simple. There is
deliberately **no** cross-table FK on ``owner_id`` — the owner table
varies by ``owner_type`` and Postgres FKs cannot target a variable
table. Nothing here verifies ``owner_id`` actually exists in its owner
table or belongs to ``company_id`` — that check is left to callers
(this increment's service only validates ``owner_type`` and
``routing_scheme`` against their enums); an orphaned row is inert
(never read without also resolving the owner) rather than unsafe.

``routing_scheme`` and ``owner_type`` are plain ``String`` columns
backed by a Python ``StrEnum`` for type safety, not a Postgres native
enum — the same choice ``BusinessIdentifier.scheme`` made, so adding
a new routing scheme (or, eventually, a new ownable table) is a
code-only change instead of an enum-altering migration.

See docs/multi-jurisdiction.md (M1.5) (theme T10).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class BankRoutingOwnerType(enum.StrEnum):
    """Which table's row ``owner_id`` points at."""

    ACCOUNT = "account"
    CONTACT = "contact"
    EMPLOYEE = "employee"
    SUPER_FUND = "super_fund"


class BankRoutingScheme(enum.StrEnum):
    """Jurisdiction-neutral bank-routing scheme families.

    ``au_bsb`` keeps the existing Australian shape available on this
    table (for owners that migrate onto it wholesale); ``other`` is
    the escape hatch for a scheme not yet enumerated here.
    """

    AU_BSB = "au_bsb"
    IBAN = "iban"
    SWIFT_BIC = "swift_bic"
    US_ABA_ROUTING = "us_aba_routing"
    UK_SORT_CODE = "uk_sort_code"
    SEPA = "sepa"
    OTHER = "other"


class BankRoutingIdentifier(CompanyScoped, Base):
    """One routing identifier for one owner row.

    Unique per ``(company_id, owner_type, owner_id, routing_scheme)``
    — an owner may carry several schemes (e.g. a local BSB *and* an
    IBAN) but only one row per scheme.
    """

    __tablename__ = "bank_routing_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "owner_type",
            "owner_id",
            "routing_scheme",
            name="uq_bank_routing_identifiers_owner_scheme",
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
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    # Free-text, validated at the service layer against
    # BankRoutingOwnerType — no cross-table FK (see module docstring).
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Free-text, validated at the service layer against
    # BankRoutingScheme — kept as String so a new scheme is a
    # code-only change (mirrors BusinessIdentifier.scheme).
    routing_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    scheme_value: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="The routing number/BSB/IBAN/sort code for this scheme.",
    )
    bic: Mapped[str | None] = mapped_column(
        String(11), comment="Optional SWIFT BIC alongside a national scheme."
    )
    account_number: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
