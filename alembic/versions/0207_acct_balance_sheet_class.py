"""accounts.balance_sheet_classification (M1.5 P1 tail).

Purely additive, following the 0198 pattern (nullable column, no default,
existing rows stay NULL). The AU CoA seed source (seed/load_au_coa.py)
already carries the current/non-current distinction in its Odoo
account_type values but previously discarded it, collapsing everything to
the flat ASSET/LIABILITY account_type. This column gives it a queryable
home; the seed loader is updated separately to populate it going forward.
Existing companies' accounts stay NULL until re-seeded — no backfill here
(re-deriving from the discarded odoo_account_type stashed in
``accounts.extra`` is a service-layer concern, not a migration's).

Reference-data only — nothing in the posting path reads this column. No
RLS change — new column on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the column.

Revision ID: 0207_acct_balance_sheet_class
Revises: 0206_asset_disposal_override
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0207_acct_balance_sheet_class"
down_revision: str | None = "0206_asset_disposal_override"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("balance_sheet_classification", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "balance_sheet_classification")
