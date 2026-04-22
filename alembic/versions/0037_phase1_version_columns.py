"""Phase 1 — add version column to accounts, companies, tax_codes

Required for the Phase 1 API extraction: every tier-1 entity needs a
monotonic version integer for If-Match optimistic locking and change_log
versioning (Phase 4.5 offline sync).

Backfills all existing rows to version = 1 in a single transaction.

Revision ID: 0037_phase1_version_columns
Revises: 0036_phase0_api_scaffolding
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_phase1_version_columns"
down_revision: str | None = "0036_phase0_api_scaffolding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("accounts", "companies", "tax_codes"):
        op.add_column(
            table,
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.execute(f"UPDATE {table} SET version = 1 WHERE version IS NULL")  # noqa: S608
        op.alter_column(table, "version", server_default=None)


def downgrade() -> None:
    for table in ("accounts", "companies", "tax_codes"):
        op.drop_column(table, "version")
