"""fixed_assets acquisition-cost component breakdown (M1.5 P1 tail).

Purely additive, following the 0198 pattern. Optional itemisation of
``fixed_assets.cost``, which remains the sole authoritative total the
depreciation/disposal math reads — these three columns are record-keeping
only, not enforced to sum to ``cost``. NULL on every existing asset.

Reference-data only — nothing in the posting path reads these columns. No
RLS change — new columns on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the three columns.

Revision ID: 0215_fixed_asset_cost_components
Revises: 0214_company_fin_year_start_day
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0215_fixed_asset_cost_components"
down_revision: str | None = "0214_company_fin_year_start_day"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_COLS = (
    "purchase_price_component",
    "duty_component",
    "installation_component",
)


def upgrade() -> None:
    for name in _COLS:
        op.add_column(
            "fixed_assets",
            sa.Column(name, sa.Numeric(18, 4), nullable=True),
        )


def downgrade() -> None:
    for name in reversed(_COLS):
        op.drop_column("fixed_assets", name)
