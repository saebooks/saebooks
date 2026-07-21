"""leave_balances.jurisdiction_code (M1.5 P1 tail).

Purely additive, following the 0198 pattern. Leave semantics (NES
annual/personal leave accrual) are implicitly AU; this column names the
jurisdiction whose statutory leave scheme governs a balance, for
companies with non-AU employees. NULL = AU, unchanged for every existing
balance.

No RLS change — new column on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the column.

Revision ID: 0212_leave_balance_jurisdiction
Revises: 0211_company_lifecycle_status
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0212_leave_balance_jurisdiction"
down_revision: str | None = "0211_company_lifecycle_status"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leave_balances",
        sa.Column("jurisdiction_code", sa.String(length=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("leave_balances", "jurisdiction_code")
