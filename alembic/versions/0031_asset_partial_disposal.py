"""Asset register v2 — parent_asset_id for partial disposal (Batch MM/3)

Adds a nullable self-referential ``parent_asset_id`` FK to
``fixed_assets``. A partial disposal splits the original asset row
into two:

1. The **parent** row has its ``cost`` and ``residual_value`` reduced
   by the disposed fraction. Status stays ``active``.
2. A **child** row is created carrying the disposed fraction's cost,
   pointing at its parent via ``parent_asset_id``, then ``dispose_asset``
   runs against the child — posting the normal closeout journal on
   just the disposed share.

``ON DELETE SET NULL`` — if a parent row is ever hard-deleted (rare;
archive is the preferred path) the child's link is dropped rather
than cascading. Since both parent + child are company-scoped the
company CASCADE still cleans them up together if the company is
deleted.

Additive migration — no backfill, no constraint tightening on
existing rows. All pre-MM/3 rows have ``parent_asset_id IS NULL``,
which is the default "not a partial-disposal child" state.

Revision ID: 0031_asset_partial_disposal
Revises: 0030_asset_register_v2_dv
Create Date: 2026-04-21
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_asset_partial_disposal"
down_revision: str | None = "0030_asset_register_v2_dv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fixed_assets",
        sa.Column(
            "parent_asset_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fixed_assets.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Self-ref — points at the original asset when this row is "
                "the disposed-fraction child from a partial disposal."
            ),
        ),
    )
    op.create_index(
        "ix_fixed_assets_parent_asset_id",
        "fixed_assets",
        ["parent_asset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_fixed_assets_parent_asset_id", "fixed_assets")
    op.drop_column("fixed_assets", "parent_asset_id")
