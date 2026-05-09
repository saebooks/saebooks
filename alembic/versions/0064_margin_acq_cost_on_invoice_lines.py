"""Add margin_acq_cost to invoice_lines for Div 75 margin-scheme GST.

Gap MOTR-1 (edge-motor-dealer): no margin-scheme tax code existed; used-vehicle
sales were charged full 10 % GST instead of 1/11 × (sale − acq). This column
stores the per-line acquisition cost so the service layer can compute the
correct margin GST.

Revision ID: 0064_margin_acq_cost
Revises: 0063_trust_account_flag
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0064_margin_acq_cost"
down_revision: str | None = "0063_trust_account_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_lines",
        sa.Column("margin_acq_cost", sa.Numeric(18, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoice_lines", "margin_acq_cost")
