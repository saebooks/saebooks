"""CoA statutory-mapping columns on companies + accounts (M1.5 · Wave 5-CoA / T10b).

Purely additive, following the 0177 pattern (nullable columns, no default,
existing rows stay NULL; validation happens at the service layer against
the reference DB, so there is deliberately NO foreign key here):

* ``companies.statutory_framework_code`` — which statutory
  chart-of-accounts framework (``statutory_account_frameworks`` in the
  reference DB) the company reports under. NULL for every AU company —
  Australia mandates no account numbering plan.
* ``accounts.statutory_account_code`` / ``statutory_account_label_local``
  / ``statutory_parent_class`` — the account's number, local-language
  label, and class under that framework. NULL when no framework applies.
* ``accounts.net_asset_restriction_tier`` — NFP / fund-accounting
  net-asset restriction tier (unrestricted / board_designated /
  donor_restricted_temporary / donor_restricted_permanent). NULL for
  for-profit books, i.e. every existing account.

Reference-data only — nothing in the posting path reads these columns.
No RLS change — new columns on existing tenant-scoped tables, not new
tables. Reversible: downgrade drops the five columns.

Revision ID: 0198_coa_statutory_columns
Revises: 0197_company_tax_registered
Create Date: 2026-07-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0198_coa_statutory_columns"
down_revision: str | None = "0197_company_tax_registered"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ACCOUNT_COLS = (
    ("statutory_account_code", sa.String(32)),
    ("statutory_account_label_local", sa.String(255)),
    ("statutory_parent_class", sa.String(64)),
    ("net_asset_restriction_tier", sa.String(32)),
)


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("statutory_framework_code", sa.String(32), nullable=True),
    )
    for name, type_ in _ACCOUNT_COLS:
        op.add_column("accounts", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_ACCOUNT_COLS):
        op.drop_column("accounts", name)
    op.drop_column("companies", "statutory_framework_code")
