"""Phase 1 — add version + item_type columns to items

* ``version`` INT NOT NULL DEFAULT 1 — optimistic-locking counter for the
  /api/v1/items If-Match protocol, consistent with 0037/0038.
* ``item_type`` VARCHAR(16) NOT NULL DEFAULT 'inventory' — discriminates
  tracked inventory items from service (non-stocked) items.

Backfills all existing rows. New DB CHECK constraint added for item_type.

Revision ID: 0039_phase1_item_version
Revises: 0038_phase1_user_version
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_phase1_item_version"
down_revision: str | None = "0038_phase1_user_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # version column
    op.add_column(
        "items",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute("UPDATE items SET version = 1 WHERE version IS NULL")  # noqa: S608
    op.alter_column("items", "version", server_default=None)

    # item_type column
    op.add_column(
        "items",
        sa.Column(
            "item_type",
            sa.String(16),
            nullable=False,
            server_default="inventory",
        ),
    )
    op.execute("UPDATE items SET item_type = 'inventory' WHERE item_type IS NULL")  # noqa: S608
    op.alter_column("items", "item_type", server_default=None)

    op.create_check_constraint(
        "ck_items_item_type_valid",
        "items",
        "item_type IN ('inventory', 'service')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_items_item_type_valid", "items", type_="check")
    op.drop_column("items", "item_type")
    op.drop_column("items", "version")
