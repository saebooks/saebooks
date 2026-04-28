"""Add is_trade_in flag to invoice_lines for motor-dealer trade-in handling.

Gap MOTR-2 (edge-motor-dealer): trade-in vehicles were recorded as negative
discount lines on the sales invoice, hiding the full new-car sale price from
G1 and omitting the trade-in acquisition from AP/inventory. This column flags
a line as a trade-in acquisition; the posting service then excludes it from the
invoice journal and auto-creates a companion AP bill instead.

Revision ID: 0071_trade_in
Revises: 0070_invoice_settlement_date
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0071_trade_in"
down_revision: str | None = "0070_invoice_settlement_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_lines",
        sa.Column(
            "is_trade_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("invoice_lines", "is_trade_in")
