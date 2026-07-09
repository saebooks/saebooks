"""Phase 1 tier-3 — add version + tenant_id to credit_notes.

Required for /api/v1/credit_notes: optimistic locking (If-Match) needs
``version INT`` and multi-tenant isolation needs ``tenant_id UUID``.

The server default on tenant_id is kept so that the legacy credit note
service can still create credit notes without explicitly passing tenant_id.
Phase 2 will drop the default once all callers are migrated.

Revision ID: 0046_credit_notes_version_tenant
Revises: 0045_payments_version_tenant
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0046_credit_notes_version_tenant"
down_revision: str | None = "0045_payments_version_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_TABLE = "credit_notes"


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
    # credit_notes — version                                               #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "version"):
        op.add_column(
            _TABLE,
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.execute(f"UPDATE {_TABLE} SET version = 1 WHERE version IS NULL")  # noqa: S608
        op.alter_column(_TABLE, "version", server_default=None)

    # ------------------------------------------------------------------ #
    # credit_notes — tenant_id                                             #
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
        # Keep the server default so legacy credit note service still works
        # without explicitly supplying tenant_id.


def downgrade() -> None:
    op.drop_constraint(f"fk_{_TABLE}_tenant_id", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
    op.drop_column(_TABLE, "version")
