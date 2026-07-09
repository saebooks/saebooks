"""companies table

Revision ID: 0002_companies
Revises: 0001_settings
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_companies"
down_revision: str | None = "0001_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("legal_name", sa.String()),
        sa.Column("trading_name", sa.String()),
        sa.Column("abn", sa.String(20)),
        sa.Column("acn", sa.String(20)),
        sa.Column("address", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("base_currency", sa.String(3), nullable=False, server_default="AUD"),
        sa.Column("fin_year_start_month", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("audit_mode", sa.String(), nullable=False, server_default="immutable"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("companies")
