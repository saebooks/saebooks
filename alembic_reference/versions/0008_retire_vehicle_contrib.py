"""retirement_vehicle_types + mandatory_contribution_rules (M1.5 · T6).

Generic retirement-vehicle + mandatory-contribution layer, additive
alongside the AU-only ``super_fund``/``super_guarantee_rates``:

* ``retirement_vehicle_types`` — per-jurisdiction local retirement/pension
  vehicle names (APRA fund, SMSF, 401(k), Traditional IRA, workplace
  pension, RRSP, ...) mapped to a jurisdiction-neutral ``canonical_bucket``
  + ``tax_treatment`` (EET/TEE/ETT/other). See ``retirement_vehicle.py``.
* ``mandatory_contribution_rules`` — per-jurisdiction mandatory
  retirement-contribution rates (payer, rate, earnings base, optional
  age-band + cap). See ``mandatory_contribution_rule.py``.

Both are reference tables (jurisdiction-keyed, not tenant-scoped) — no RLS.

See docs/multi-jurisdiction.md (M1.5) (theme T6, gap K4).

Revision ID: 0006_retirement_vehicle_and_contributions
Revises: 0007_income_corp_cap_bik
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_retire_vehicle_contrib"
down_revision: str | None = "0007_income_corp_cap_bik"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retirement_vehicle_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("local_name", sa.String(128), nullable=False),
        sa.Column("canonical_bucket", sa.String(32), nullable=False),
        sa.Column("tax_treatment", sa.String(16), nullable=False),
        sa.Column("description", sa.String(512)),
        sa.UniqueConstraint(
            "jurisdiction", "code", name="uq_retirement_vehicle_types_jur_code"
        ),
    )

    op.create_table(
        "mandatory_contribution_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("payer", sa.String(16), nullable=False),
        sa.Column("rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("earnings_base", sa.String(64), nullable=False),
        sa.Column("age_band", postgresql.JSONB),
        sa.Column("cap_amount", sa.Numeric(14, 2)),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_mandatory_contribution_rules_jur_code_eff",
        ),
    )


def downgrade() -> None:
    op.drop_table("mandatory_contribution_rules")
    op.drop_table("retirement_vehicle_types")
