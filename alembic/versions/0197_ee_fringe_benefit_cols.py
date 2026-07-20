"""pay_run_lines — EE fringe-benefit (erisoodustus) columns (kmd-inf-tsd
follow-up, Packet 2).

``services.fringe_benefits_ee`` (this packet) computes the company-car
(kW/age) and generic cash-value fringe-benefit tax events, but
``pay_run_lines`` had nowhere to persist the result — same gap class
0191_ee_payroll_compute_cols closed for ordinary wage withholding.
Three additive, nullable-or-defaulted columns, mirroring that
migration's split between explicit scalar totals (so GL posting/TSD can
read them cleanly, per 0191's own stated preference) and an itemized
JSONB list (so more than one benefit — e.g. a car AND a housing benefit
— can sit on one pay-run line, mirroring the existing
``allowances``/``deductions`` JSONB columns on this same table):

- ``ee_fringe_benefits``            — JSONB list of
  ``EEFringeBenefitResult``-shaped dicts (one per benefit event this
  line carries). NOT NULL, default ``[]`` — same convention as
  ``allowances``/``deductions``/``paid_leave_lines``.
- ``ee_fringe_benefit_income_tax``  — NUMERIC(14,2), NULL. Sum of
  ``income_tax`` across ``ee_fringe_benefits``. NULL when the line
  carries no fringe benefit (the common case) — distinguishes "no
  benefit" from "benefit worth EUR 0.00", same NULL-means-not-computed
  posture as ``ee_income_tax`` etc. on this table.
- ``ee_fringe_benefit_social_tax``  — NUMERIC(14,2), NULL. Sum of
  ``social_tax`` across ``ee_fringe_benefits``.

All three NULL/empty for every existing row and every line the EE
compute path does not attach a fringe benefit to (AU lines never touch
them at all). Fully reversible via ``op.drop_column``.

Chains from the current company-DB single head ``0196_ee_filing_ref_cols``
(verified: no other file's ``down_revision`` names it — see that
migration's own note; the suite pins ``len(get_heads()) == 1``).

Revision ID: 0197_ee_fringe_benefit_cols
Revises:     0196_ee_filing_ref_cols
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0197_ee_fringe_benefit_cols"
down_revision: str | None = "0196_ee_filing_ref_cols"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "pay_run_lines"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "ee_fringe_benefits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
            comment=(
                "Itemized EE fringe-benefit events for this line "
                "(services.fringe_benefits_ee.EEFringeBenefitResult-"
                "shaped dicts). [] when the line carries none."
            ),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "ee_fringe_benefit_income_tax",
            sa.Numeric(14, 2),
            nullable=True,
            comment=(
                "Sum of income_tax across ee_fringe_benefits. NULL when "
                "the line carries no fringe benefit."
            ),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "ee_fringe_benefit_social_tax",
            sa.Numeric(14, 2),
            nullable=True,
            comment=(
                "Sum of social_tax across ee_fringe_benefits. NULL when "
                "the line carries no fringe benefit."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "ee_fringe_benefit_social_tax")
    op.drop_column(_TABLE, "ee_fringe_benefit_income_tax")
    op.drop_column(_TABLE, "ee_fringe_benefits")
