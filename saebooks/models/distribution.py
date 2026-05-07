"""Trust distribution models.

TrustDistribution — one row per year-end resolution (or interim).
BeneficiaryEntitlement — one row per beneficiary slice of a distribution.

The status lifecycle is DRAFT -> MINUTED -> POSTED.  POSTED means a
matching journal entry has been created and linked via journal_entry_id.
"""
import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
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

# Default tenant uuid (migration 0040 seed); keeps single-tenant
# constructors working. tenant_id + RLS added by migration 0083.
_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class DistributionStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    MINUTED = "MINUTED"
    POSTED = "POSTED"


class TrustDistribution(CompanyScoped, Base):
    __tablename__ = "trust_distributions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    financial_year: Mapped[int] = mapped_column(Integer, nullable=False)
    distribution_date: Mapped[date] = mapped_column(Date, nullable=False)
    resolution_minuted_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[DistributionStatus] = mapped_column(
        String(16), nullable=False, default=DistributionStatus.DRAFT
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Franking credits (PRTR-4): total imputation credits attached to the
    # trust's dividend income for this distribution period.  The grossed-up
    # distributable income = total_amount + total_franking_credits.
    total_franking_credits: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # FK set once the JE is created — null until POSTED.
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    entitlements: Mapped[list["BeneficiaryEntitlement"]] = relationship(
        back_populates="distribution",
        cascade="all, delete-orphan",
        order_by="BeneficiaryEntitlement.sort_order",
    )

    @property
    def grossed_up_income(self) -> Decimal:
        """Cash distributable income + imputation credits (s97 gross-up)."""
        return self.total_amount + self.total_franking_credits


class BeneficiaryEntitlement(Base):
    __tablename__ = "beneficiary_entitlements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    distribution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trust_distributions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    beneficiary_name: Mapped[str] = mapped_column(String, nullable=False)
    # percentage (0.00 – 100.00); must sum to 100 across a distribution.
    percentage: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Per-beneficiary franking credit share (PRTR-4).  Set by the service
    # as percentage% of total_franking_credits.  Used in s97 statements.
    franking_credit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    # Payable account (e.g. "Beneficiary Payable – Alice") — nullable so
    # the form can be saved without forcing the user to pre-create accounts.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(256))

    # Optional link to a Contact row of type BENEFICIARY. Null-safe — the
    # beneficiary_name string is the authoritative display value; this FK is
    # for reporting joins only.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )

    distribution: Mapped[TrustDistribution] = relationship(back_populates="entitlements")

    @property
    def grossed_up_entitlement(self) -> Decimal:
        """Beneficiary's grossed-up amount: cash + imputation credit."""
        return self.amount + self.franking_credit_amount
