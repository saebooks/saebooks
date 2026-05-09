"""Add retention_pct to bill_lines for civil construction AP retention.

Gap CIVL-3 (medium-civil-contractor): no retention_pct field on bill lines;
controller had to track 5% retention holdback in a spreadsheet. This column
stores the per-line retention percentage so the posting pipeline can split
Cr AP into Trade Creditors (net payable) and Retentions Payable (withheld).

Also adds the Retentions Payable account (2-1850) to the seed CoA so a fresh
install has the account available without a manual re-seed.

Revision ID: 0067_retention_pct_bill_lines
Revises: 0066_retention_pct
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0067_retention_pct_bill_lines"
down_revision: str | None = "0066_retention_pct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bill_lines",
        sa.Column(
            "retention_pct",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("bill_lines", "retention_pct")
