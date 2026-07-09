"""Add tracking_vehicle_id to bill_lines for per-VIN floorplan cost tagging.

Floorplan interest bills could only be coded
to a GL account; no way to tag a line to a specific vehicle (VIN / stock no.).
This column enables per-unit gross-margin reporting net of floorplan interest.

Revision ID: 0075_tracking_vehicle_id
Revises: 0074_tpar_supplier_flag
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0075_tracking_vehicle_id"
down_revision: str | None = "0074_tpar_supplier_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bill_lines",
        sa.Column("tracking_vehicle_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bill_lines", "tracking_vehicle_id")
