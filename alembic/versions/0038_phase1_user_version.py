"""Phase 1 — add version column to users

Users was not included in 0037 (accounts/companies/tax_codes).
Adding it now for the /api/v1/users router (cycle 4).

Revision ID: 0038_phase1_user_version
Revises: 0037_phase1_version_columns
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_phase1_user_version"
down_revision: str | None = "0037_phase1_version_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute("UPDATE users SET version = 1 WHERE version IS NULL")  # noqa: S608
    op.alter_column("users", "version", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "version")
