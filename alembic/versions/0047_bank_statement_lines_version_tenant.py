"""Phase 1 tier-4 — add version + tenant_id + archived_at + balance to bank_statement_lines.

Also adds IGNORED to the StatementLineStatus enum.

Required for /api/v1/bank_statement_lines: optimistic locking (If-Match)
needs ``version INT`` and multi-tenant isolation needs ``tenant_id UUID``.
Soft-archive needs ``archived_at``. Running balance needs ``balance``.

Revision ID: 0047_bsl_tenant_version
Revises: 0046_credit_notes_version_tenant
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0047_bsl_tenant_version"
down_revision: str | None = "0046_credit_notes_version_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_TABLE = "bank_statement_lines"


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
    # ------------------------------------------------------------------ #
    # bank_statement_lines — version                                       #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "version"):
        op.add_column(
            _TABLE,
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.execute(f"UPDATE {_TABLE} SET version = 1 WHERE version IS NULL")  # noqa: S608
        op.alter_column(_TABLE, "version", server_default=None)

    # ------------------------------------------------------------------ #
    # bank_statement_lines — tenant_id                                     #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "tenant_id"):
        op.add_column(
            _TABLE,
            sa.Column(
                "tenant_id",
                PG_UUID(as_uuid=False),
                nullable=False,
                server_default=sa.text(f"'{_DEFAULT_TENANT}'"),
            ),
        )
        op.execute(
            sa.text(f"UPDATE {_TABLE} SET tenant_id = '{_DEFAULT_TENANT}'")  # noqa: S608
        )
        op.create_foreign_key(
            f"fk_{_TABLE}_tenant_id",
            _TABLE,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        # Keep the server default so legacy bank feed import still works
        # without explicitly supplying tenant_id.

    # ------------------------------------------------------------------ #
    # bank_statement_lines — archived_at                                   #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "archived_at"):
        op.add_column(
            _TABLE,
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )

    # ------------------------------------------------------------------ #
    # bank_statement_lines — balance (running balance, nullable)           #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "balance"):
        op.add_column(
            _TABLE,
            sa.Column("balance", sa.Numeric(14, 2), nullable=True),
        )


def downgrade() -> None:
    op.drop_constraint(f"fk_{_TABLE}_tenant_id", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
    op.drop_column(_TABLE, "version")
    op.drop_column(_TABLE, "archived_at")
    op.drop_column(_TABLE, "balance")
