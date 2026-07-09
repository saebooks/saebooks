"""Phase 1 tier-3 — add version + tenant_id to journal_entries.

Required for the /api/v1/journal_entries endpoint: optimistic locking
(If-Match) needs ``version INT`` and multi-tenant isolation needs
``tenant_id UUID``.

Revision ID: 0042_journal_entries_version_tenant
Revises: 0041_add_tenant_id
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0042_je_version_tenant"
down_revision: str | None = "0041_add_tenant_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_TABLE = "journal_entries"


def upgrade() -> None:
    # --- version column ---
    op.add_column(
        _TABLE,
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute(f"UPDATE {_TABLE} SET version = 1 WHERE version IS NULL")  # noqa: S608
    op.alter_column(_TABLE, "version", server_default=None)

    # --- archived_at column (soft-delete / void support) ---
    op.add_column(
        _TABLE,
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- tenant_id column ---
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
    # Note: We intentionally keep the server default on tenant_id here so that
    # the legacy journal service (services/journal.py) can still create
    # journal_entries without explicitly supplying tenant_id.  The API service
    # (services/journal_entries.py) always passes tenant_id explicitly.
    # Phase 2 will drop the default once all callers are migrated.


def downgrade() -> None:
    op.drop_constraint(f"fk_{_TABLE}_tenant_id", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
    op.drop_column(_TABLE, "archived_at")
    op.drop_column(_TABLE, "version")
