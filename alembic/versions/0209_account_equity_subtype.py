"""accounts.equity_subtype (M1.5 P1 tail).

Purely additive, following the 0198/0207/0208 pattern (nullable column, no
default, existing rows stay NULL). ``AccountType`` has a single EQUITY
value; this column carries the for-profit sub-classification (share
capital / retained earnings / reserves / drawings / other). The seed
loader is updated separately to populate it for the AU equity accounts.

Reference-data only — nothing in the posting path reads this column. No
RLS change — new column on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the column.

Revision ID: 0209_account_equity_subtype
Revises: 0208_acct_contra_normal_bal
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0209_account_equity_subtype"
down_revision: str | None = "0208_acct_contra_normal_bal"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("equity_subtype", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "equity_subtype")
