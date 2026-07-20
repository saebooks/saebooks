"""Canonical per-jurisdiction employee/employer social-insurance schemes
(M1.5 · T7).

AU's Medicare levy is one instance of a much broader family: a mandatory
wage-based contribution collected alongside payroll, sometimes by
withholding (US FICA Social Security/Medicare, UK employee National
Insurance) and sometimes by year-end assessment (AU Medicare levy). This
table generalises that family so other jurisdictions' social-insurance
schemes can be represented without inventing an AU-shaped table per
country.

The AU-named table this generalised, ``medicare_levy``, had zero
consumers and no seed data, and was dropped (M1.5 Wave 3a rename sweep).

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T7).
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class ContributionPayer(enum.StrEnum):
    """Who is liable for the contribution."""

    EMPLOYEE = "employee"
    EMPLOYER = "employer"
    BOTH = "both"


class CollectionMechanism(enum.StrEnum):
    """How the contribution is actually collected."""

    PAYROLL_WITHHOLDING = "payroll_withholding"  # withheld from each pay run
    ASSESSMENT = "assessment"                     # settled via year-end tax assessment


class SocialContributionScheme(ReferenceBase):
    """A named social-insurance/contribution scheme in one jurisdiction,
    active over a date range. NOT per-company — payroll services pick a
    code from here."""

    __tablename__ = "social_contribution_schemes"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_social_contribution_schemes_jur_code_eff",
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
        comment="Stable per-jurisdiction code, e.g. 'au_medicare', 'us_fica_ss', 'uk_employee_ni'.",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    payer: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of ContributionPayer — who the rate applies to.",
    )
    rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Rate as a percentage (2.0000 = 2%, not 0.02).",
    )
    wage_base_cap: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment="Annual wage base the rate stops applying above. NULL = uncapped.",
    )
    wage_base_floor: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment=(
            "Minimum wage base the rate is assessed against regardless of "
            "actual wages paid (e.g. EE sotsiaalmaks EUR 886/mo). "
            "NULL = no floor. Added kmd-inf-tsd scope Packet 3 — "
            "closes the gap social_contribution_schemes.yaml's header "
            "used to flag."
        ),
    )
    collection_mechanism: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="One of CollectionMechanism — how the contribution is collected.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
