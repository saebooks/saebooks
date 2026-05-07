"""account ranges — configurable top-level code prefixes

Revision ID: 0008_account_ranges
Revises: 0007_bank_statement_lines
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_account_ranges"
down_revision: str | None = "0007_bank_statement_lines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_ranges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prefix",
            sa.String(16),
            nullable=False,
            comment="Top-level code prefix (e.g. '1', '10', '200')",
        ),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column(
            "account_types",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            comment="Allowed AccountType values for this range",
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("company_id", "prefix", name="uq_account_ranges_company_prefix"),
    )
    op.create_index("ix_account_ranges_company_id", "account_ranges", ["company_id"])

    # Add settings (defaults for new installs)
    op.execute(
        "INSERT INTO settings (key, value) VALUES ('structured_numbering', 'true')"
        " ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO settings (key, value) VALUES ('prefix_mode', '\"classic\"')"
        " ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key IN ('structured_numbering', 'prefix_mode')")
    op.drop_index("ix_account_ranges_company_id", table_name="account_ranges")
    op.drop_table("account_ranges")
