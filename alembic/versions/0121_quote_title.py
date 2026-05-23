"""Add title column to quotes table.

A short human-readable heading for the quote — e.g. the project name.
Surfaced on the /quotes list page, quote detail page, edit form, and
PDF render (\\ProjectName). Nullable; quotes created before this
migration have NULL title until backfilled or edited.

Revision ID: 0121_quote_title
Revises: 0120_payg_tax_scales_fy25_26
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0121_quote_title"
down_revision: str | None = "0120_payg_tax_scales_fy25_26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quotes",
        sa.Column("title", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quotes", "title")
