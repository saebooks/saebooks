"""SQL query history for the admin browser SQL tool

Revision ID: 0015_sql_queries
Revises: 0014_bank_rules
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_sql_queries"
down_revision: str | None = "0014_bank_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sql_queries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column(
            "row_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Rows returned (or affected — but writes are rejected)",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "error",
            sa.Text(),
            comment="If the query failed, the error message",
        ),
        sa.Column("performed_by", sa.String(64)),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sql_queries_executed",
        "sql_queries",
        ["executed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sql_queries_executed", table_name="sql_queries")
    op.drop_table("sql_queries")
