"""Inventory v1 — items table + item_id FK on invoice_lines + bill_lines

Revision ID: 0028_inventory
Revises: 0027_foreign_currency
Create Date: 2026-04-21

Scope (Batch GG/3, MVP):

* ``items`` — a per-company tracked-stock product. SKU + name, costing
  method (WAC only for v1), on_hand_qty + weighted-average unit cost
  in base currency, and the three GL accounts that receipts / sales /
  cost-of-goods post against.
* ``invoice_lines`` + ``bill_lines`` each get a nullable ``item_id``
  FK (``ON DELETE SET NULL``). A line with ``item_id`` set is a stock
  movement: bills *receive* (Dr Inventory, updates WAC), invoices
  *issue* (Dr COGS / Cr Inventory at WAC, in addition to the normal
  Dr AR / Cr Income on the header line). Items without ``item_id`` are
  service-only lines and behave exactly as before.

Design decisions:

* Cost method is a CHECK constraint (``WAC`` only for v1) rather than
  an enum so we can extend to ``FIFO`` / ``STANDARD`` without a
  downgrade-unfriendly enum alter. The Python layer enforces the
  allowed set.
* ``on_hand_qty`` + ``wac_cost`` are ``Numeric(18, 4)`` — inventory
  reporting needs more granularity than money's two places because
  unit costs are frequently sub-cent (fasteners, bulk wire, fluid).
  Rounded to two places only when posting the GL journal.
* Three account FKs (inventory / COGS / income) are ``RESTRICT`` so an
  admin can't archive a CoA account that still has stock pointing at
  it. ``archived_at`` soft-delete on the item row mirrors Contact;
  hard-delete would orphan historical invoice/bill lines.

Everything is additive — no backfill, no constraint changes to
existing rows. Existing invoice/bill lines stay at ``item_id = NULL``
which is exactly the pre-GG/3 behaviour.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_inventory"
down_revision: str | None = "0027_foreign_currency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COST_METHODS = ("WAC",)


def upgrade() -> None:
    # --- items ------------------------------------------------------------
    op.create_table(
        "items",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "cost_method",
            sa.String(16),
            nullable=False,
            server_default="WAC",
        ),
        sa.Column(
            "on_hand_qty",
            sa.Numeric(18, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "wac_cost",
            sa.Numeric(18, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "default_sale_price",
            sa.Numeric(18, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "inventory_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "cogs_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "income_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "extra",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("company_id", "sku", name="uq_items_company_sku"),
        sa.CheckConstraint(
            "cost_method IN ('" + "', '".join(COST_METHODS) + "')",
            name="ck_items_cost_method_valid",
        ),
    )
    op.create_index(
        "ix_items_company_active",
        "items",
        ["company_id"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    # --- item_id FKs on line tables --------------------------------------
    # SET NULL on delete — never lose GL history when an admin archives
    # an item (though archival is soft-delete, so this is only hit if
    # someone hard-deletes via SQL).
    for table in ("invoice_lines", "bill_lines"):
        op.add_column(
            table,
            sa.Column(
                "item_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("items.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_item_id",
            table,
            ["item_id"],
            postgresql_where=sa.text("item_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("invoice_lines", "bill_lines"):
        op.drop_index(f"ix_{table}_item_id", table_name=table)
        op.drop_column(table, "item_id")
    op.drop_index("ix_items_company_active", table_name="items")
    op.drop_table("items")
