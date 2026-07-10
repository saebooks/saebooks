"""capital_gains_tax_regimes + corporate_tax_rates + dividend_relief_mechanisms
+ benefit_in_kind_rates canonical tables (M1.5 · T11).

The engine had no representation for how a jurisdiction relieves capital
gains (discount/indexation/rollover/exemption), what its corporate income
tax rate structure is by entity scope, how it relieves double taxation of
dividends (imputation/franking/classical/...), or non-cash employment
benefits taxed on something other than AU's FBT shape. This adds four
jurisdiction-neutral canonical reference tables, all additive alongside
the existing AU-shaped tables (``income_tax_brackets``, ``fbt_rates``) —
NOT a rename, NOT a replacement. See each model module for the full
rationale.

All four are reference tables (jurisdiction-keyed, not tenant-scoped) —
no RLS.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T11,
domain "Income, corporate & capital taxes").

Revision ID: 0007_income_corp_cap_bik
Revises: 0006_duty_concessions
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_income_corp_cap_bik"
down_revision: str | None = "0006_duty_concessions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capital_gains_tax_regimes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column(
            "taxpayer_type",
            sa.String(24),
            nullable=False,
            server_default="any",
        ),
        sa.Column("relief_mechanism", sa.String(16), nullable=False),
        sa.Column("relief_rate_or_schedule", postgresql.JSONB),
        sa.Column("holding_period_threshold_days", sa.Integer),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "taxpayer_type", "relief_mechanism", "effective_from",
            name="uq_capital_gains_tax_regimes_jur_tp_mech_eff",
        ),
    )

    op.create_table(
        "corporate_tax_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("sub_jurisdiction", sa.String(8)),
        sa.Column("tax_year", sa.Integer, nullable=False),
        sa.Column("entity_scope", sa.String(32), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "tax_year", "entity_scope",
            name="uq_corporate_tax_rates_jur_subjur_year_scope",
            # sub_jurisdiction is nullable — NULLs-not-distinct so a
            # national-only row (sub_jurisdiction=NULL) upserts idempotently
            # via the seed loader's ON CONFLICT instead of duplicating on
            # every reload. Requires Postgres 15+ (this project runs
            # postgres:16).
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "dividend_relief_mechanisms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("mechanism_type", sa.String(16), nullable=False),
        sa.Column("credit_or_exemption_rate", sa.Numeric(7, 4)),
        sa.Column(
            "refundable", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "mechanism_type", "effective_from",
            name="uq_dividend_relief_mechanisms_jur_type_eff",
        ),
    )

    op.create_table(
        "benefit_in_kind_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("benefit_category", sa.String(32), nullable=False),
        sa.Column("incidence", sa.String(16), nullable=False),
        sa.Column("valuation_method", sa.String(24), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("filing_period_start_month", sa.Integer, nullable=False),
        sa.Column("filing_period_end_month", sa.Integer, nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "benefit_category", "effective_from",
            name="uq_benefit_in_kind_rates_jur_category_eff",
        ),
        sa.CheckConstraint(
            "filing_period_start_month BETWEEN 1 AND 12",
            name="ck_benefit_in_kind_rates_start_month",
        ),
        sa.CheckConstraint(
            "filing_period_end_month BETWEEN 1 AND 12",
            name="ck_benefit_in_kind_rates_end_month",
        ),
    )


def downgrade() -> None:
    op.drop_table("benefit_in_kind_rates")
    op.drop_table("dividend_relief_mechanisms")
    op.drop_table("corporate_tax_rates")
    op.drop_table("capital_gains_tax_regimes")
