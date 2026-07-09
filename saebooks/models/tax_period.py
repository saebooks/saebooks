"""Tax-return periods per company per jurisdiction."""
import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class TaxPeriodType(enum.StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    BIMONTHLY = "bimonthly"
    SIX_MONTHLY = "six_monthly"
    ANNUAL = "annual"


class TaxPeriodStatus(enum.StrEnum):
    OPEN = "open"
    LOCKED = "locked"
    LODGED = "lodged"


class TaxPeriod(CompanyScoped, Base):
    __tablename__ = "tax_periods"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "jurisdiction", "period_start",
            name="uq_tax_periods_company_jur_start",
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
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    # Free text rather than FK — jurisdictions live in the reference DB,
    # validated at the service layer. See docs/multi-jurisdiction.md.
    jurisdiction: Mapped[str] = mapped_column(String(3), nullable=False)
    period_type: Mapped[TaxPeriodType] = mapped_column(
        Enum(
            TaxPeriodType,
            name="tax_period_type",
            values_callable=lambda et: [e.value for e in et],
        ),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[TaxPeriodStatus] = mapped_column(
        Enum(
            TaxPeriodStatus,
            name="tax_period_status",
            values_callable=lambda et: [e.value for e in et],
        ),
        nullable=False,
        default=TaxPeriodStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
