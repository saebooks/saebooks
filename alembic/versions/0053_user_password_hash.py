"""B/43 — add password_hash to users table.

The column is nullable so existing Authentik-only users are unaffected.
A non-NULL value enables login via POST /api/v1/auth/login.

Format: ``pbkdf2sha256$<iterations>$<salt_hex>$<hash_hex>``

Revision ID: 0053_user_password_hash
Revises: 0052_bsl_reconciliation
Create Date: 2026-04-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053_user_password_hash"
down_revision: str | None = "0052_bsl_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _col_exists(table: str, col: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": col},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if not _col_exists("users", "password_hash"):
        op.add_column(
            "users",
            sa.Column("password_hash", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("users", "password_hash")
