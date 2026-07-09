"""Add gst_registered and gst_effective_date to companies table.

No GST registration effective-date
field in company settings; POST silently discarded extra fields.

Revision ID: 0065_gst_fields
Revises: 0064_margin_acq_cost
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0065_gst_fields"
down_revision: str | None = "0064_margin_acq_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "gst_registered",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "companies",
        sa.Column("gst_effective_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "gst_effective_date")
    op.drop_column("companies", "gst_registered")
