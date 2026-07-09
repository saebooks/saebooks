"""Add retention_pct to invoice_lines for civil construction retention billing.

No retention_pct field on invoice lines;
negative-line workaround reduced gross revenue and GST simultaneously by $22k,
breaking the audit trail. This column stores the per-line retention percentage
so the posting pipeline can split Dr AR into Trade Debtors (net payable) and
Retentions Receivable (withheld portion).

Revision ID: 0066_retention_pct
Revises: 0065_gst_fields
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0066_retention_pct"
down_revision: str | None = "0065_gst_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_lines",
        sa.Column(
            "retention_pct",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("invoice_lines", "retention_pct")
