"""tax_codes table

Revision ID: 0004_tax_codes
Revises: 0003_accounts
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_tax_codes"
down_revision: str | None = "0003_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tax_codes",
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
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("rate", sa.Numeric(6, 3), nullable=False, server_default="0"),
        sa.Column("tax_system", sa.String(16), nullable=False, server_default="GST"),
        sa.Column(
            "reporting_type", sa.String(32), nullable=False, server_default="taxable"
        ),
        sa.Column("description", sa.String()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_tax_codes_company_id", "tax_codes", ["company_id"])
    # Partial unique: code must be unique per company among active (non-archived) rows
    op.create_index(
        "uq_tax_codes_company_code_active",
        "tax_codes",
        ["company_id", "code"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tax_codes_company_code_active", table_name="tax_codes")
    op.drop_index("ix_tax_codes_company_id", table_name="tax_codes")
    op.drop_table("tax_codes")
