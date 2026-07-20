"""Per-jurisdiction benefit-in-kind (BIK) rates — generalises FBT (M1.5 · T11).

AU Fringe Benefits Tax is one instance of a broader family: many
jurisdictions tax non-cash employment benefits (a company car, health
insurance, housing, entertainment), but *who* is taxed on them differs —
AU taxes the employer directly (FBT); many other jurisdictions instead
add the benefit's value to the employee's taxable wages (employee-taxed),
and some split liability (hybrid). This table generalises that family so
a non-AU jurisdiction's benefit-in-kind rules can be represented without
inventing an AU-shaped table per country.

The AU-named table this generalised, ``fbt_rate`` / ``FbtRate``, had zero
consumers and no seed data, and was dropped (M1.5 Wave 3a rename sweep —
K7 in the audit); AU FBT is seeded into this table as one
``benefit_category`` row.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T11,
domain "Income, corporate & capital taxes").
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class BenefitInKindIncidence(enum.StrEnum):
    """Who the benefit's tax liability falls on."""

    EMPLOYER_TAXED = "employer_taxed"  # employer pays a benefit-level tax (AU FBT)
    EMPLOYEE_TAXED = "employee_taxed"  # benefit value added to employee's taxable wages
    HYBRID = "hybrid"                  # liability split between employer and employee


BENEFIT_IN_KIND_INCIDENCES = tuple(i.value for i in BenefitInKindIncidence)


class BenefitInKindValuationMethod(enum.StrEnum):
    """How the taxable value of the benefit is determined."""

    STATUTORY_FORMULA = "statutory_formula"  # fixed statutory % of a base value (AU car statutory method)
    COST_BASIS = "cost_basis"                # actual employer cost of providing the benefit
    MARKET_VALUE = "market_value"            # open-market value of the benefit
    ACTUAL_COST = "actual_cost"              # AU FBT "operating cost" method — logged actual running costs
    AMOUNT_PER_UNIT = "amount_per_unit"      # EE company car: EUR/kW/month (0012_bik_amount_per_unit)


BENEFIT_IN_KIND_VALUATION_METHODS = tuple(m.value for m in BenefitInKindValuationMethod)


class BenefitInKindRate(ReferenceBase):
    """A benefit-in-kind rate/rule in force in one jurisdiction over a
    date range. NOT per-company — payroll/FBT-equivalent services pick a
    row from here keyed by jurisdiction and benefit_category."""

    __tablename__ = "benefit_in_kind_rates"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "benefit_category", "effective_from",
            name="uq_benefit_in_kind_rates_jur_category_eff",
        ),
        CheckConstraint(
            "filing_period_start_month BETWEEN 1 AND 12",
            name="ck_benefit_in_kind_rates_start_month",
        ),
        CheckConstraint(
            "filing_period_end_month BETWEEN 1 AND 12",
            name="ck_benefit_in_kind_rates_end_month",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    benefit_category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Free-text category, e.g. 'motor_vehicle', 'entertainment', 'housing', 'car_parking'.",
    )
    incidence: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of BENEFIT_IN_KIND_INCIDENCES — who the tax liability falls on.",
    )
    valuation_method: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="One of BENEFIT_IN_KIND_VALUATION_METHODS — how the taxable value is determined.",
    )
    rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Rate as a percentage (47.0000 = 47% for AU FBT) applied to the valued benefit.",
    )
    # --- 0012_bik_amount_per_unit (EE Packet 2, company-car erisoodustus) - #
    # For an amount-shaped (not percentage-shaped) valuation, e.g. EE's
    # EUR-per-kW-per-month company-car basis. Both NULL for every
    # percentage-shaped row (rate_percent carries the rate there instead);
    # ``services.fringe_benefits_ee`` reads these two for the car case and
    # does NOT consult rate_percent for it (rate_percent is still populated
    # on those rows too, for schema consistency with the seeded ``general``
    # row — see the migration docstring).
    rate_amount_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    rate_unit: Mapped[str | None] = mapped_column(
        String(32),
        comment="Unit label for rate_amount_per_unit, e.g. 'eur_per_kw_per_month'.",
    )
    # ------------------------------------------------------------------- #
    filing_period_start_month: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-12. AU FBT year runs 1 April-31 March, so 4.",
    )
    filing_period_end_month: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-12. AU FBT year runs 1 April-31 March, so 3.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
