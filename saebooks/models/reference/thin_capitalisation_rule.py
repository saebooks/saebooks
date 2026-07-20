"""Per-jurisdiction thin-capitalisation / interest-limitation parameters
(M1.5 · Wave 5-Income).

Before this table nothing in the engine represented how a jurisdiction
limits interest (debt) deductions: AU's post-2023 fixed-ratio test (30%
of tax EBITDA, group-ratio and third-party-debt elections, 15-year
carry-forward of denied amounts), the pre-2023 safe-harbour debt:equity
ratios, EU ATAD Article 4 style EBITDA rules, or capital-based rules for
banks/ADIs.

This is the reference *parameter* table only — per-company disallowed
-interest *balances* (the audit's proposed
``disallowed_interest_carryforwards`` company-DB table) are a
transactional tracking concern for a later slice, not reference data.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (domain
"Income, corporate & capital taxes", "Thin capitalisation / interest
expense limitation").
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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


class ThinCapMechanicType(enum.StrEnum):
    """Jurisdiction-neutral interest-limitation mechanics. Every local
    test resolves to one of these so the engine can reason about the
    limitation without knowing the local scheme name."""

    FIXED_RATIO_EBITDA = "fixed_ratio_ebitda"          # % of (tax) EBITDA (AU FRT, ATAD art 4)
    SAFE_HARBOUR_DEBT_RATIO = "safe_harbour_debt_ratio"  # max debt:equity (or debt:asset) multiple
    GROUP_RATIO = "group_ratio"                        # worldwide-group ratio election
    ARMS_LENGTH_DEBT = "arms_length_debt"              # arm's-length debt amount test
    THIRD_PARTY_DEBT = "third_party_debt"              # external-debt-only election (AU post-2023)
    CAPITAL_BASED = "capital_based"                    # minimum-capital rules for banks/ADIs
    NONE = "none"                                      # no thin-cap regime


THIN_CAP_MECHANIC_TYPES = tuple(m.value for m in ThinCapMechanicType)


class RefThinCapitalisationRule(ReferenceBase):
    """One interest-limitation mechanic in force for one jurisdiction /
    entity scope over a date range. NOT per-company — income-tax services
    pick a row from here keyed by the company's jurisdiction and entity
    class (general investor vs financial entity vs ADI/bank)."""

    __tablename__ = "thin_capitalisation_rules"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "entity_scope", "mechanic_type", "effective_from",
            name="uq_thin_capitalisation_rules_jur_scope_mech_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    entity_scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="general",
        server_default="general",
        comment=(
            "Entity class the mechanic applies to (mirrors "
            "CorporateTaxRate.entity_scope), e.g. 'general', "
            "'financial_entity', 'adi', 'any'."
        ),
    )
    mechanic_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of THIN_CAP_MECHANIC_TYPES.",
    )
    fixed_ratio_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4),
        comment=(
            "For fixed_ratio_ebitda: deduction cap as a percentage of the "
            "ratio base (AU FRT 30.0000 = 30% of tax EBITDA)."
        ),
    )
    safe_harbour_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4),
        comment=(
            "For safe_harbour_debt_ratio: maximum debt multiple of the "
            "ratio base (AU pre-2023 general 1.5:1 debt:equity, financial "
            "entities 15:1)."
        ),
    )
    ratio_base: Mapped[str | None] = mapped_column(
        String(24),
        comment=(
            "What the ratio is measured against, e.g. 'tax_ebitda', "
            "'accounting_ebitda', 'equity', 'assets'."
        ),
    )
    de_minimis_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment=(
            "Debt-deduction amount below which the regime does not apply "
            "(AU AUD 2,000,000). NULL = no de-minimis carve-out."
        ),
    )
    group_ratio_election_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="True when a worldwide-group-ratio election may replace the default test.",
    )
    disallowed_carryforward_years: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Years a denied interest deduction may be carried forward and "
            "re-tested (AU FRT 15). NULL = denied amounts are permanently "
            "lost."
        ),
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
