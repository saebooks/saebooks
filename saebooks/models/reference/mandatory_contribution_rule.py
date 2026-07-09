"""Per-jurisdiction mandatory retirement/social-insurance contribution rules
(M1.5 · T6).

AU's Superannuation Guarantee rate lives under an AU noun
(``super_guarantee_rates`` — FY + flat rate only, see
``super_guarantee_rate.py``). That shape does not capture *who* pays
(employer/employee/both), *what* the rate applies to (ordinary time
earnings vs gross wages vs pensionable earnings), age-based carve-outs
(e.g. a minimum-age threshold), or a contribution cap — all of which vary
by jurisdiction: US 401(k) has an IRS annual elective-deferral cap, UK
auto-enrolment has both employer and employee minimum rates plus an
earnings-band trigger, CA RRSP has an annual contribution-room cap. This
table generalises that family so any jurisdiction's mandatory contribution
regime can be represented without inventing an AU-shaped table per country.

This is a distinct concept from ``social_contribution_scheme`` (M1.5 · T7,
general wage-based social insurance like Medicare levy / FICA / National
Insurance): a mandatory *retirement* contribution is earmarked to a
retirement vehicle (see ``retirement_vehicle.py``), not consolidated
revenue. The two tables are kept separate and independently module-owned
so neither's future evolution forces a change to the other.

Additive only — ``super_guarantee_rates`` is untouched; a future,
coordinated pass may migrate AU onto this table (see K4 in the audit).

See docs/multi-jurisdiction.md (M1.5) (theme T6, gap K4).
"""
import enum
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class MandatoryContributionPayer(enum.StrEnum):
    """Who is liable for the mandatory contribution."""

    EMPLOYER = "employer"
    EMPLOYEE = "employee"
    BOTH = "both"


class MandatoryContributionRule(ReferenceBase):
    """A named mandatory retirement-contribution rule in one jurisdiction,
    active over a date range. NOT per-company — payroll/retirement services
    pick a code from here (AU today via the Superannuation Guarantee rate;
    generically once K4's coordinated generalisation lands)."""

    __tablename__ = "mandatory_contribution_rules"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_mandatory_contribution_rules_jur_code_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Stable per-jurisdiction code, e.g. 'au_super_guarantee', 'us_401k_deferral_cap', 'uk_auto_enrolment'.",
    )
    payer: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of MandatoryContributionPayer — who the rate applies to.",
    )
    rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Rate as a percentage (11.5000 = 11.5%, not 0.115).",
    )
    earnings_base: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Free-text earnings base the rate applies to, e.g. 'ordinary_time_earnings', 'gross_wages', 'pensionable_earnings'.",
    )
    age_band: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Optional age-based carve-out, e.g. {'min_age': 18} or {'min_age': 18, 'max_age': 69}. NULL = no age restriction.",
    )
    cap_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment="Optional contribution cap (annual, in the jurisdiction's currency). NULL = uncapped.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
