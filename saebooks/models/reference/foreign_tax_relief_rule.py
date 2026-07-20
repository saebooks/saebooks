"""Per-jurisdiction foreign-tax relief (double-taxation) rules (M1.5 ·
Wave 5-Income).

Before this table nothing in the engine represented how a jurisdiction
relieves double taxation of foreign-source income: AU's foreign income
tax offset (an ordinary credit limited to the Australian tax on the
doubly-taxed amounts, with a AUD 1,000 de-minimis short-cut and no
carry-forward), classical exemption systems, exemption-with-progression,
or a bare deduction for foreign tax paid.

This is the reference *rule* table only — per-company credit *balances*
(the audit's proposed ``foreign_tax_credit_balances`` company-DB table)
are a transactional tracking concern for a later slice, not reference
data.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (domain
"Income, corporate & capital taxes", "Foreign tax credit / relief for
double taxation").
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class ForeignTaxReliefMethod(enum.StrEnum):
    """Jurisdiction-neutral double-taxation relief methods. Every local
    scheme resolves to one of these so the engine can reason about the
    relief without knowing the local scheme name."""

    ORDINARY_CREDIT = "ordinary_credit"    # credit capped at domestic tax on the foreign income (AU FITO)
    FULL_CREDIT = "full_credit"            # credit for the full foreign tax paid, uncapped
    EXEMPTION = "exemption"                # foreign income wholly exempt
    EXEMPTION_WITH_PROGRESSION = "exemption_with_progression"  # exempt, but counted for rate-setting
    DEDUCTION = "deduction"                # foreign tax merely deductible from taxable income
    NONE = "none"                          # no unilateral relief


FOREIGN_TAX_RELIEF_METHODS = tuple(m.value for m in ForeignTaxReliefMethod)


class RefForeignTaxReliefRule(ReferenceBase):
    """The double-taxation relief rule in force for one jurisdiction /
    taxpayer type / income basket over a date range. NOT per-company —
    income-tax services pick a row from here keyed by the company's
    jurisdiction and the basket the foreign income falls in."""

    __tablename__ = "foreign_tax_relief_rules"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "taxpayer_type", "income_basket", "effective_from",
            name="uq_foreign_tax_relief_rules_jur_tp_basket_eff",
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
        comment="e.g. 'company', 'individual', 'any' (mirrors CapitalGainsTaxRegime).",
    )
    relief_method: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of FOREIGN_TAX_RELIEF_METHODS.",
    )
    income_basket: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="any",
        server_default="any",
        comment=(
            "Schedular basket the rule applies to where the jurisdiction "
            "baskets foreign income (US 'passive'/'general'); 'any' when "
            "unbasketed (AU post-2008 FITO)."
        ),
    )
    offset_de_minimis_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment=(
            "Relief claimable without computing the limitation (AU AUD "
            "1,000 FITO short-cut). NULL = no de-minimis short-cut."
        ),
    )
    carry_forward_years: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Years excess relief may be carried forward (US 10; AU FITO "
            "0 — excess offset is lost). NULL = indefinite."
        ),
    )
    carry_back_years: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Years excess relief may be carried back (US 1; AU 0).",
    )
    limitation_formula: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        comment=(
            "Shape of the relief cap, e.g. AU FITO {'limit': "
            "'domestic_tax_on_double_taxed_amounts'}. JSONB so richer "
            "per-basket formulas are not a schema change. NULL = uncapped."
        ),
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
