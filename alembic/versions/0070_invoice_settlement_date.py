"""Add settlement_date to invoices for real-estate commission BAS timing.

Commissions were posted to the BAS
period of the invoice issue date. Real estate commissions are earned at
unconditional exchange or settlement, which can be weeks to months later.
This nullable column lets operators record the exchange/settlement date;
post_invoice uses it as the GL entry_date when present.

Revision ID: 0070_invoice_settlement_date
Revises: 0069_allocation_rules
Create Date: 2026-04-28
"""
import sqlalchemy as sa

from alembic import op

revision: str = "0070_invoice_settlement_date"
down_revision: str | None = "0069_allocation_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("settlement_date", sa.Date, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoices", "settlement_date")
