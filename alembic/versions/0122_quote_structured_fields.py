"""Add structured fields to quotes + quote_lines for engineering-quote rendering.

Adds columns that the existing model stuffs into the description text:
  * quotes.scope          — free-form project scope paragraph (shown in PROJECT DETAILS box on the PDF)
  * quote_lines.section_label — name of the section this line belongs to (Overleaf-style grouping)
  * quote_lines.material  — material spec (eg "250UB37", "Colorbond 0.48 BMT")
  * quote_lines.length_note — approximate-lengths note (eg "6.2–6.4 m")
  * quote_lines.drawing_ref — drawing reference (eg "DD-A-20-01-5, K-12317 DD2 (S01)")

All five are nullable; existing rows keep NULL until backfilled or edited.

Required so the rendered quote PDF can match the Overleaf .tex design with a
6-column section-grouped table.

Revision ID: 0122_quote_structured_fields
Revises: 0121_quote_title
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0122_quote_structured_fields"
down_revision: str | None = "0121_quote_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("scope", sa.Text(), nullable=True))
    op.add_column("quote_lines", sa.Column("section_label", sa.String(length=255), nullable=True))
    op.add_column("quote_lines", sa.Column("material", sa.String(length=255), nullable=True))
    op.add_column("quote_lines", sa.Column("length_note", sa.String(length=255), nullable=True))
    op.add_column("quote_lines", sa.Column("drawing_ref", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("quote_lines", "drawing_ref")
    op.drop_column("quote_lines", "length_note")
    op.drop_column("quote_lines", "material")
    op.drop_column("quote_lines", "section_label")
    op.drop_column("quotes", "scope")
