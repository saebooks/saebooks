"""EE payroll compute columns (kmd-inf-tsd scope Packet 3).

Two additive, nullable deltas needed so a posted EE pay run actually
carries EE withholding instead of AU PAYG/super (scope §0's second
finding — the pay-run engine was AU-only; §2.2's "8 of 9 Lisa-1 fields
missing or wrong"):

1. ``pay_run_lines`` — five new nullable columns for the EE compute's
   output (``services.pay_runs_v2._compute_ee``). The existing
   ``tax``/``super_amount`` scalars are AU PAYG/super specifically and
   do NOT fit EE's five distinct components (scope §3.2's own framing:
   "the AU tax/super_amount scalars don't fit"); explicit columns
   (chosen over a JSONB blob per the scope's stated preference "so TSD
   can read them cleanly"):
     - ee_income_tax              (22% over the basic exemption)
     - ee_unemployment_employee   (1.6%)
     - ee_unemployment_employer   (0.8%)
     - ee_social_tax              (33%, EUR 886/mo floor)
     - ee_pillar_ii               (2/4/6% elective)
   All NULL for every existing (AU) pay-run line and every AU line
   computed going forward — the AU path never writes them.

2. ``employees`` — two nullable per-employee EE payroll elections the
   compute path reads:
     - ee_pillar_ii_rate_percent   (2.00 / 4.00 / 6.00; NULL -> the
       seeded statutory default, 2%)
     - ee_basic_exemption_elected  (whether THIS employer applies the
       EUR 700/mo basic exemption; NULL -> treated as NOT applied, the
       tax-safe default — matches ``services.payroll_ee``'s
       ``apply_exemption = basic_exemption_elected is True`` and
       ``models/employee.py``'s field docstring. Critic round 1 fix:
       this note and the column ``comment=`` below previously said the
       opposite, "NULL -> True")

Nullable throughout -> no server_default, no backfill. Fully reversible
via ``op.drop_column``. Chains from the current company-DB head,
``0190_contact_registration_number`` (verified via
``grep -rl down_revision.*0190`` returning nothing before this file was
added — 0190 was still a leaf).

Revision ID: 0191_ee_payroll_compute_cols
Revises:     0190_contact_registration_number
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0191_ee_payroll_compute_cols"
down_revision: str | None = "0190_contact_registration_number"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_PRL_TABLE = "pay_run_lines"
_PRL_COLUMNS = (
    "ee_income_tax",
    "ee_unemployment_employee",
    "ee_unemployment_employer",
    "ee_social_tax",
    "ee_pillar_ii",
)

_EMP_TABLE = "employees"


def upgrade() -> None:
    for col in _PRL_COLUMNS:
        op.add_column(
            _PRL_TABLE,
            sa.Column(col, sa.Numeric(14, 2), nullable=True),
        )
    op.add_column(
        _EMP_TABLE,
        sa.Column(
            "ee_pillar_ii_rate_percent",
            sa.Numeric(5, 2),
            nullable=True,
            comment=(
                "Employee-elected EE pillar-II (kohustuslik "
                "kogumispension) contribution rate — 2.00 / 4.00 / "
                "6.00. NULL = the seeded statutory default (2%)."
            ),
        ),
    )
    op.add_column(
        _EMP_TABLE,
        sa.Column(
            "ee_basic_exemption_elected",
            sa.Boolean(),
            nullable=True,
            comment=(
                "Whether this employer applies the EE EUR 700/mo "
                "basic exemption (maksuvaba tulu) to this employee's "
                "income-tax withholding. NULL treated as NOT applied "
                "(tax-safe default — matches services.payroll_ee)."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_EMP_TABLE, "ee_basic_exemption_elected")
    op.drop_column(_EMP_TABLE, "ee_pillar_ii_rate_percent")
    for col in reversed(_PRL_COLUMNS):
        op.drop_column(_PRL_TABLE, col)
