"""Per-jurisdiction tax-loss carry-forward / carry-back rules (M1.5 ·
Wave 5-Income).

Before this table nothing in the engine represented how a jurisdiction
lets a taxpayer use a tax loss: whether losses carry forward (and for how
long), whether they carry back, whether an annual offset cap applies
(Germany's Mindestbesteuerung), whether the loss is quarantined to its
own schedular basket (AU capital losses only offset capital gains), and
what continuity tests gate deduction (AU's continuity-of-ownership /
business-continuity tests, trust-loss tests).

This is the reference *rule* table only — per-company loss *balances*
(the audit's proposed ``tax_loss_balances`` company-DB table) are a
transactional tracking concern for a later slice, not reference data.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (domain
"Income, corporate & capital taxes", "Tax loss carry-forward /
carry-back").
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class LossBasket(enum.StrEnum):
    """Jurisdiction-neutral schedular loss baskets. Every local basket
    resolves to one of these so the engine can reason about quarantining
    without knowing the local scheme name."""

    REVENUE = "revenue"      # ordinary/trading losses
    CAPITAL = "capital"      # capital losses (AU: quarantined to capital gains)
    FOREIGN = "foreign"      # foreign-source losses where separately basketed
    PASSIVE = "passive"      # passive-income losses where separately basketed
    SPECIFIED = "specified"  # any other jurisdiction-specific schedular basket


LOSS_BASKETS = tuple(b.value for b in LossBasket)


class RefTaxLossCarryoverRule(ReferenceBase):
    """The loss-usage rule in force for one jurisdiction / taxpayer type /
    loss basket over a date range. NOT per-company — income-tax services
    pick a row from here keyed by the company's jurisdiction and the
    basket a loss arose in."""

    __tablename__ = "tax_loss_carryover_rules"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "taxpayer_type", "loss_basket", "effective_from",
            name="uq_tax_loss_carryover_rules_jur_tp_basket_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    taxpayer_type: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="any",
        server_default="any",
        comment="e.g. 'company', 'individual', 'trust', 'any' (mirrors CapitalGainsTaxRegime).",
    )
    loss_basket: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of LOSS_BASKETS.",
    )
    carry_forward_years: Mapped[int | None] = mapped_column(
        Integer,
        comment="Years a loss may be carried forward. NULL = indefinite (AU); 0 = no carry-forward.",
    )
    carry_back_years: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Years a loss may be carried back (AU currently 0).",
    )
    annual_offset_cap_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        comment=(
            "Cap on the share of current-year income a carried loss may "
            "offset, as a percentage (DE Mindestbesteuerung 60.00). NULL = "
            "uncapped."
        ),
    )
    offset_cap_threshold_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment=(
            "Income amount below which the annual offset cap does not "
            "apply (DE EUR 1,000,000). NULL = cap (if any) applies from "
            "the first unit of income."
        ),
    )
    quarantined_to_basket: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "True when losses in this basket may only offset gains/income "
            "of the same basket (AU capital losses)."
        ),
    )
    continuity_tests: Mapped[list[str] | None] = mapped_column(
        JSONB,
        comment=(
            "Ordered list of test codes gating loss deduction, e.g. AU "
            "company ['continuity_of_ownership', 'business_continuity'], "
            "AU trust ['fifty_percent_stake', 'pattern_of_distributions', "
            "'control', 'income_injection']. NULL = no continuity test."
        ),
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
