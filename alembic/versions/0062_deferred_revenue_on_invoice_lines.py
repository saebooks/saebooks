"""Add service_start_date, service_end_date, recognized_through_date to invoice_lines.

Revision ID: 0062
Revises: 0061
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0062_deferred_revenue"
down_revision = "0061_franking_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_lines",
        sa.Column("service_start_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("service_end_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("recognized_through_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoice_lines", "recognized_through_date")
    op.drop_column("invoice_lines", "service_end_date")
    op.drop_column("invoice_lines", "service_start_date")
