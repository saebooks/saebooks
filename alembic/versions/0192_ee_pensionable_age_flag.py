"""employees.ee_pensionable_age (kmd-inf-tsd scope Packet 3, follow-up).

The task named "the EUR 700/EUR 776 universal exemption" explicitly —
0191_ee_payroll_compute_cols only wired the EUR 700 standard figure
plus an apply/don't-apply election, dropping the EUR 776
pensionable-age variant on the floor. Rather than invent an unsourced
old-age-pension-age threshold to derive pensionable status from
``Employee.dob`` (flagged as a real gap in
``services.payroll_ee``'s module docstring), this exposes the fact as
an explicit per-employee flag — same pattern as
``ee_pillar_ii_rate_percent``: caller-supplied ground truth, not a
derived guess.

Additive, nullable, no backfill, no server_default. NULL = not
pensionable-age (uses the EUR 700 standard exemption when elected).
Fully reversible via ``op.drop_column``. Chains from
``0191_ee_payroll_compute_cols`` (current head at the time this
follow-up was written — still a leaf before this file was added).

Revision ID: 0192_ee_pensionable_age_flag
Revises:     0191_ee_payroll_compute_cols
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0192_ee_pensionable_age_flag"
down_revision: str | None = "0191_ee_payroll_compute_cols"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "employees"
_COLUMN = "ee_pensionable_age"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=True,
            comment=(
                "Whether this employee is pensionable-age for EE "
                "basic-exemption purposes (EUR 776/mo code 650 vs "
                "EUR 700/mo code 610). NULL/False = standard EUR 700."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
