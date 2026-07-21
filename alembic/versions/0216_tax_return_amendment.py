"""tax_returns amendment linkage (M1.5 P1 tail).

Purely additive, following the 0198 pattern. ``TaxReturnStatus.AMENDED``
already exists but had no supersedes/amended-by link. ``supersedes_return_id``
is a self-FK (SET NULL on delete — an amendment record must never be
blocked from being removed, or vice versa) set on the NEW (correcting)
return, pointing at the original it supersedes. NULL for every existing
return.

No RLS change — new columns on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the two columns.

Revision ID: 0216_tax_return_amendment
Revises: 0215_fixed_asset_cost_components
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0216_tax_return_amendment"
down_revision: str | None = "0215_fixed_asset_cost_components"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_returns",
        sa.Column(
            "supersedes_return_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tax_returns.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "tax_returns", sa.Column("amendment_reason", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tax_returns", "amendment_reason")
    op.drop_column("tax_returns", "supersedes_return_id")
