"""Asset-disposal gain/loss account override on companies (M1.5 P1 tail).

Purely additive, following the 0198/0204 pattern (nullable columns, no
default, existing rows stay NULL — every existing AU company keeps
resolving to the hardcoded "4-9100"/"6-9100" codes it always did via
``services.assets.dispose_asset``'s fallback).

``companies.asset_disposal_gain_account_code`` /
``asset_disposal_loss_account_code`` — optional account-code override,
mirroring ``ar_control_account_code``/``ap_control_account_code`` (0198).
NULL = engine falls back to the AU convention codes.

No RLS change — new columns on an existing tenant-scoped table, not a
new table. Reversible: downgrade drops the two columns.

Revision ID: 0206_asset_disposal_override
Revises: 0205_drop_company_acn
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0206_asset_disposal_override"
down_revision: str | None = "0205_drop_company_acn"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("asset_disposal_gain_account_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("asset_disposal_loss_account_code", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "asset_disposal_loss_account_code")
    op.drop_column("companies", "asset_disposal_gain_account_code")
