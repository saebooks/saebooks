"""Phase 1 tier-4 — add version, tenant_id, archived_at to budgets.

Required for /api/v1/budgets: optimistic locking (If-Match) needs
``version INT``, multi-tenant isolation needs ``tenant_id UUID``,
and soft-archive via DELETE needs ``archived_at``.

Revision ID: 0051_budgets_tv
Revises: 0050_ri_version_tenant
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0051_budgets_tv"
down_revision: str | None = "0050_ri_version_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_TABLE = "budgets"


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
    # budgets — version                                                    #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "version"):
        op.add_column(
            _TABLE,
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.execute(f"UPDATE {_TABLE} SET version = 1 WHERE version IS NULL")  # noqa: S608
        op.alter_column(_TABLE, "version", server_default=None)

    # ------------------------------------------------------------------ #
    # budgets — tenant_id                                                  #
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
        # Keep server default so legacy code inserting without tenant_id still works.

    # ------------------------------------------------------------------ #
    # budgets — archived_at                                                #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "archived_at"):
        op.add_column(
            _TABLE,
            sa.Column(
                "archived_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column(_TABLE, "archived_at")
    op.drop_constraint(f"fk_{_TABLE}_tenant_id", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
    op.drop_column(_TABLE, "version")
