"""accounts.is_contra + accounts.normal_balance (M1.5 P1 tail).

Purely additive, following the 0198/0207 pattern. ``is_contra`` defaults
false (server_default) so every existing row is unaffected; ``normal_balance``
is nullable with no default (existing rows stay NULL until re-seeded). The
seed loader is updated separately to populate both going forward.

Reference-data only — nothing in the posting path reads these columns. No
RLS change — new columns on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the columns.

Revision ID: 0208_acct_contra_normal_bal
Revises: 0207_acct_balance_sheet_class
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0208_acct_contra_normal_bal"
down_revision: str | None = "0207_acct_balance_sheet_class"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "is_contra",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "accounts",
        sa.Column("normal_balance", sa.String(length=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "normal_balance")
    op.drop_column("accounts", "is_contra")
