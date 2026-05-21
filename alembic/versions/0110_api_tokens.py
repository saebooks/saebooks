"""api_tokens: machine bearer tokens for CLI / MCP / third-party

Revision ID: 0110_api_tokens
Revises: 0109_time_entries
Create Date: 2026-05-22

Adds the ``api_tokens`` table backing ``models/api_token.py`` and
``services/api_tokens.py``. See model docstring for the rationale.

Why a partial index on the active set: 99% of verify-path lookups
hit ``WHERE token_prefix = $1 AND revoked_at IS NULL`` (after the
unique constraint dedups by prefix). The partial keeps that hot
index small even after thousands of revoked tokens accumulate.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0110_api_tokens"
down_revision = "0109_time_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "company_id",
            sa.UUID(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("token_prefix", sa.String(6), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(60), nullable=False),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_api_tokens_company_id", "api_tokens", ["company_id"]
    )
    op.create_index(
        "ix_api_tokens_user_id", "api_tokens", ["user_id"]
    )
    op.create_index(
        "ix_api_tokens_token_prefix", "api_tokens", ["token_prefix"], unique=True
    )
    op.create_index(
        "ix_api_tokens_company_user", "api_tokens", ["company_id", "user_id"]
    )
    op.create_index(
        "ix_api_tokens_active",
        "api_tokens",
        ["company_id", "user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_api_tokens_active", table_name="api_tokens")
    op.drop_index("ix_api_tokens_company_user", table_name="api_tokens")
    op.drop_index("ix_api_tokens_token_prefix", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_company_id", table_name="api_tokens")
    op.drop_table("api_tokens")
