"""PAYG withholding + STSL coefficient ref tables.

These are global reference data (NOT CompanyScoped, NOT RLS-gated) —
the rows describe ATO Schedule 1 (NAT 1004) and study-loan repayment
coefficients which are identical for every tenant in the country.

See migration ``0112_payg_tables.py`` for the seeded values + the
"DERIVED — verify before production" caveats.

Consumer: ``saebooks.services.payg``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


PAYG_PERIODS = ("WEEKLY", "FORTNIGHTLY", "MONTHLY")


class PaygTaxScale(Base):
    """One band of the ATO Schedule 1 PAYG withholding formula.

    A "scale" (1–6 per NAT 1004, plus 7 = WHM, 8 = non-resident no-TFN
    in our internal numbering) is the set of bands sharing a scale_no.
    Each band is selected by earnings range; coefficients give
    ``WH = a*x - b`` where ``x = floor(weekly_earnings) + 0.99`` per
    the NAT 1004 formula.
    """

    __tablename__ = "payg_tax_scales"
    __table_args__ = (
        CheckConstraint(
            "earnings_ceil IS NULL OR earnings_ceil > earnings_floor",
            name="ck_payg_tax_scales_band_ascending",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_payg_tax_scales_dates_ascending",
        ),
        CheckConstraint(
            "scale_no >= 1 AND scale_no <= 8",
            name="ck_payg_tax_scales_scale_range",
        ),
        CheckConstraint(
            "coef_a >= 0",
            name="ck_payg_tax_scales_coef_a_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scale_no: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[str] = mapped_column(
        Enum(*PAYG_PERIODS, name="payg_period_enum", create_type=False),
        nullable=False,
    )
    earnings_floor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    earnings_ceil: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    coef_a: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    coef_b: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source_doc: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StslCoefficient(Base):
    """STSL (study/training support loan) repayment coefficient band.

    Applied **additively** to the base PAYG withholding when the
    employee has flagged ``study_training_support_loan = True``.
    """

    __tablename__ = "stsl_coefficients"
    __table_args__ = (
        CheckConstraint(
            "earnings_ceil IS NULL OR earnings_ceil > earnings_floor",
            name="ck_stsl_coefficients_band_ascending",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_stsl_coefficients_dates_ascending",
        ),
        CheckConstraint(
            "coef_a >= 0",
            name="ck_stsl_coefficients_coef_a_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period: Mapped[str] = mapped_column(
        Enum(*PAYG_PERIODS, name="payg_period_enum", create_type=False),
        nullable=False,
    )
    earnings_floor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    earnings_ceil: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    coef_a: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    coef_b: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source_doc: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
