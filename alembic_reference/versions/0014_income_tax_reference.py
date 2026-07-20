"""Income-tax reference tables (M1.5 · Wave 5-Income).

Purely additive, following the T3/T4 pattern (new reference tables +
AU seed; no posting-path change, no company-DB change):

* ``capital_gains_event_types`` — statutory capital-gains event catalogue
  (AU ITAA 1997 s 104-5: A1 disposal, C1 loss/destruction, K7 ...).
* ``tax_loss_carryover_rules`` — loss carry-forward/carry-back rules per
  jurisdiction / taxpayer type / schedular basket (AU: indefinite
  carry-forward, capital losses quarantined, COT/BCT continuity tests).
* ``thin_capitalisation_rules`` — interest-limitation parameters per
  jurisdiction / entity scope (AU post-2023 fixed-ratio 30% of tax
  EBITDA; pre-2023 safe-harbour debt:equity rows date-ranged out).
* ``foreign_tax_relief_rules`` — double-taxation relief rules (AU FITO:
  ordinary credit, AUD 1,000 de-minimis, no carry-forward).

The company-DB transaction/balance tables the audit sketches alongside
these (``capital_gain_events``, ``tax_loss_balances``,
``disallowed_interest_carryforwards``, ``foreign_tax_credit_balances``)
are deliberately NOT in this slice — they track per-company positions,
not reference data.

Reversible: downgrade drops the four tables.

Revision ID: 0014_income_tax_reference
Revises: 0013_coa_statutory_frameworks
Create Date: 2026-07-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_income_tax_reference"
down_revision: str | None = "0013_coa_statutory_frameworks"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capital_gains_event_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("statutory_reference", sa.String(64)),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "code",
            name="uq_capital_gains_event_types_jur_code",
        ),
    )

    op.create_table(
        "tax_loss_carryover_rules",
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
        sa.Column("loss_basket", sa.String(16), nullable=False),
        sa.Column("carry_forward_years", sa.Integer()),
        sa.Column(
            "carry_back_years",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("annual_offset_cap_pct", sa.Numeric(5, 2)),
        sa.Column("offset_cap_threshold_amount", sa.Numeric(14, 2)),
        sa.Column(
            "quarantined_to_basket",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("continuity_tests", postgresql.JSONB()),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.UniqueConstraint(
            "jurisdiction", "taxpayer_type", "loss_basket", "effective_from",
            name="uq_tax_loss_carryover_rules_jur_tp_basket_eff",
        ),
    )

    op.create_table(
        "thin_capitalisation_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column(
            "entity_scope",
            sa.String(32),
            nullable=False,
            server_default="general",
        ),
        sa.Column("mechanic_type", sa.String(32), nullable=False),
        sa.Column("fixed_ratio_pct", sa.Numeric(7, 4)),
        sa.Column("safe_harbour_ratio", sa.Numeric(7, 4)),
        sa.Column("ratio_base", sa.String(24)),
        sa.Column("de_minimis_threshold", sa.Numeric(14, 2)),
        sa.Column(
            "group_ratio_election_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("disallowed_carryforward_years", sa.Integer()),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.UniqueConstraint(
            "jurisdiction", "entity_scope", "mechanic_type", "effective_from",
            name="uq_thin_capitalisation_rules_jur_scope_mech_eff",
        ),
    )

    op.create_table(
        "foreign_tax_relief_rules",
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
        sa.Column("relief_method", sa.String(32), nullable=False),
        sa.Column(
            "income_basket",
            sa.String(24),
            nullable=False,
            server_default="any",
        ),
        sa.Column("offset_de_minimis_amount", sa.Numeric(14, 2)),
        sa.Column("carry_forward_years", sa.Integer()),
        sa.Column(
            "carry_back_years",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("limitation_formula", postgresql.JSONB()),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.UniqueConstraint(
            "jurisdiction", "taxpayer_type", "income_basket", "effective_from",
            name="uq_foreign_tax_relief_rules_jur_tp_basket_eff",
        ),
    )


def downgrade() -> None:
    op.drop_table("foreign_tax_relief_rules")
    op.drop_table("thin_capitalisation_rules")
    op.drop_table("tax_loss_carryover_rules")
    op.drop_table("capital_gains_event_types")
