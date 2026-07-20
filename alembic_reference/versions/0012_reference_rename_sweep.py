"""Reference-table rename/drop hygiene sweep (M1.5 Wave 3a, Class A + the
stamp-duty-rate Class-A-adjacent item — see
~/records/saebooks/m15-finish-plan.md Deliverable 2).

Every item here was verified live (grep across saebooks/, mcp/,
mcp-community/, capture_app/, platform_app/, preaccounting_app/, cli/,
scripts/, tests/) before this migration was written — zero-consumer
claims are not assumed, they are re-confirmed. The Estonia lodgement lane
(``services/lodgement/{kmd,kmd_2027,tsd,oss_q,adapters/ee*}``) is out of
scope and untouched.

DROP (canonical successor already live + AU-seeded; zero service/api/
router consumers of the old table; zero seed data of its own):
  * ``medicare_levy``            -> successor ``social_contribution_schemes``
  * ``payg_withholding_scales``  -> successor ``withholding_tables``
    (NOTE: the *operative* AU withholding path used by live payroll is a
    DIFFERENT, untouched model — ``models/payg.py:PaygTaxScale``,
    consumed by ``services/payg.py`` — this drop does not touch it.)
  * ``fbt_rates``                -> successor ``benefit_in_kind_rates``

RENAME (no canonical successor exists; zero consumers; value-preserving
table rename so the AU noun becomes a jurisdiction-neutral one and the
seed slot is preserved for non-AU rows later):
  * ``gst_registration_threshold`` -> ``consumption_tax_registration_threshold``
  * ``bsb_directory``               -> ``bank_routing_directory``

RENAME (no canonical successor; HAS 2 live consumers —
``saebooks/services/dutiable_events.py`` (``lookup_stamp_duty_rate``) and
the docstrings in ``saebooks/models/dutiable_transaction_event.py``,
updated in the same commit as this migration so nothing breaks):
  * ``stamp_duty_rates`` -> ``duty_rate_schedules``

None of the six tables carry AU seed data (verified: no
``saebooks/seeds/jurisdictions/AU/*.yaml`` declares any of these six as
its ``table:``), so there is no data-migration/backfill step and no seed
YAML needs its ``table:`` field updated.

Reversible: downgrade recreates the three dropped tables exactly as
``0001_initial_reference_schema`` defined them (empty — this DB is
seed-loaded separately) and renames the three renamed tables back.

Revision ID: 0012_reference_rename_sweep
Revises:     0011_oss_member_state_rates
Create Date: 2026-07-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_reference_rename_sweep"
down_revision: str | None = "0011_oss_member_state_rates"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ---- DROP (canonical successor already live + AU-seeded) ----
    op.drop_table("medicare_levy")
    op.drop_table("payg_withholding_scales")
    op.drop_table("fbt_rates")

    # ---- RENAME (no successor; value-preserving) ----
    op.rename_table(
        "gst_registration_threshold", "consumption_tax_registration_threshold"
    )
    op.rename_table("bsb_directory", "bank_routing_directory")
    op.rename_table("stamp_duty_rates", "duty_rate_schedules")


def downgrade() -> None:
    # ---- reverse RENAME ----
    op.rename_table("duty_rate_schedules", "stamp_duty_rates")
    op.rename_table("bank_routing_directory", "bsb_directory")
    op.rename_table(
        "consumption_tax_registration_threshold", "gst_registration_threshold"
    )

    # ---- reverse DROP (recreate empty, exactly as 0001 defined them) ----
    op.create_table(
        "fbt_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fy_year", sa.Integer, nullable=False, unique=True),
        sa.Column("fbt_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("type1_gross_up", sa.Numeric(7, 4), nullable=False),
        sa.Column("type2_gross_up", sa.Numeric(7, 4), nullable=False),
        sa.Column("statutory_interest_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("car_parking_threshold", sa.Numeric(14, 2), nullable=False),
    )

    op.create_table(
        "payg_withholding_scales",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "jurisdiction", sa.String(3), sa.ForeignKey("jurisdictions.code"),
            nullable=False,
        ),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("scale_number", sa.Integer, nullable=False),
        sa.Column("weekly_earnings_lower", sa.Numeric(12, 2), nullable=False),
        sa.Column("weekly_earnings_upper", sa.Numeric(12, 2)),
        sa.Column("a_coefficient", sa.Numeric(10, 6), nullable=False),
        sa.Column("b_subtractor", sa.Numeric(12, 2), nullable=False),
    )

    op.create_table(
        "medicare_levy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fy_year", sa.Integer, nullable=False),
        sa.Column("taxpayer_type", sa.String(32), nullable=False),
        sa.Column("threshold_no_levy", sa.Numeric(14, 2), nullable=False),
        sa.Column("threshold_full_levy", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("surcharge_brackets", postgresql.JSONB),
    )
