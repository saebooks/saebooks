"""Phase 1 tier-3 — add version + tenant_id to bills.

Required for /api/v1/bills: optimistic locking (If-Match) needs
``version INT`` and multi-tenant isolation needs ``tenant_id UUID``.

Also backfills missing FX / inventory columns added to the model
(currency, fx_rate, base_subtotal, base_tax_total, base_total,
base_amount_paid) and the project_id / item_id columns on bill_lines —
these were added to models/bill.py in later batches but the migration
was never written.

The server default on tenant_id is kept so that the legacy bill service
(services/bills.py) can still create bills without explicitly passing
tenant_id.  Phase 2 will drop the default once all callers are migrated.

Revision ID: 0044_bills_version_tenant
Revises: 0043_invoices_version_tenant
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0044_bills_version_tenant"
down_revision: str | None = "0043_invoices_version_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_TABLE = "bills"
_LINES_TABLE = "bill_lines"


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
    # bills — version                                                     #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "version"):
        op.add_column(
            _TABLE,
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.execute(f"UPDATE {_TABLE} SET version = 1 WHERE version IS NULL")  # noqa: S608
        op.alter_column(_TABLE, "version", server_default=None)

    # ------------------------------------------------------------------ #
    # bills — tenant_id                                                   #
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
        # Keep the server default so legacy bill service still works without
        # explicitly supplying tenant_id.

    # ------------------------------------------------------------------ #
    # bills — FX columns (added in Batch GG/2 to the model, backfill)    #
    # ------------------------------------------------------------------ #
    for col_name, col_def in [
        ("currency", sa.Column("currency", sa.String(3), nullable=False, server_default="AUD")),
        ("fx_rate", sa.Column("fx_rate", sa.Numeric(18, 8), nullable=False, server_default="1")),
        ("base_subtotal", sa.Column("base_subtotal", sa.Numeric(18, 2), nullable=False, server_default="0")),
        ("base_tax_total", sa.Column("base_tax_total", sa.Numeric(18, 2), nullable=False, server_default="0")),
        ("base_total", sa.Column("base_total", sa.Numeric(18, 2), nullable=False, server_default="0")),
        ("base_amount_paid", sa.Column("base_amount_paid", sa.Numeric(18, 2), nullable=False, server_default="0")),
    ]:
        if not _col_exists(_TABLE, col_name):
            op.add_column(_TABLE, col_def)

    # ------------------------------------------------------------------ #
    # bill_lines — project_id + item_id (backfill if missing)            #
    # ------------------------------------------------------------------ #
    if not _col_exists(_LINES_TABLE, "project_id"):
        op.add_column(
            _LINES_TABLE,
            sa.Column(
                "project_id",
                PG_UUID(as_uuid=False),
                sa.ForeignKey("projects.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if not _col_exists(_LINES_TABLE, "item_id"):
        op.add_column(
            _LINES_TABLE,
            sa.Column(
                "item_id",
                PG_UUID(as_uuid=False),
                sa.ForeignKey("items.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    # bill_lines extras
    op.drop_column(_LINES_TABLE, "item_id")
    op.drop_column(_LINES_TABLE, "project_id")
    # bills FX
    for col in ("base_amount_paid", "base_total", "base_tax_total", "base_subtotal", "fx_rate", "currency"):
        op.drop_column(_TABLE, col)
    # bills core
    op.drop_constraint(f"fk_{_TABLE}_tenant_id", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
    op.drop_column(_TABLE, "version")
