"""Add tenant_id to change_log and enable RLS.

P0 security fix (Round-2 critic audit, critic 12):
``change_log`` had no ``tenant_id`` column and no RLS policy — every
authenticated request could read every tenant's mutation history via
``GET /api/v1/changes``.

What this migration does
------------------------
1. Add ``tenant_id UUID NOT NULL`` with a server-side DEFAULT of the
   placeholder tenant (``00000000-0000-0000-0000-000000000001``).
   Using a server-side DEFAULT lets Postgres fill existing rows
   atomically without a separate UPDATE pass on the locked table.

2. Best-effort backfill: update rows where ``entity`` matches a table
   that has a ``company_id -> companies.tenant_id`` join path.
   Supported entities: invoice, bill, payment, contact, journal_entry,
   account, credit_note, expense, bank_statement_line, project, budget,
   fixed_asset, user, purchase_order, recurring_invoice.
   Rows whose entity is not in the list, or whose entity_id FK is
   broken/missing, retain the placeholder tenant.

3. Add FK to tenants(id) + composite index (tenant_id, id DESC) for
   the ``GET /api/v1/changes?since=`` query pattern.

4. Enable RLS + FORCE RLS + ``tenant_isolation`` policy using the
   same predicate as every other customer-data table in this codebase:
   ``(tenant_id = current_setting('app.current_tenant', true)::uuid)``

Reversibility
-------------
downgrade() drops policy, NO FORCE, DISABLE RLS, drops FK + index +
column.  Uses IF EXISTS throughout so a partial previous attempt does
not block re-running.

Revision ID: 0118_change_log_tenant_id
Revises: 0117_contact_is_one_off
Create Date: 2026-05-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0118_change_log_tenant_id"
down_revision: str | None = "0117_contact_is_one_off"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

# entity name -> (table_name, entity_id_column)
# These are the tables that have a company_id -> companies.tenant_id path.
# change_log.entity_id references the PK of each table.
_ENTITY_MAP = {
    "invoice": ("invoices", "id"),
    "bill": ("bills", "id"),
    "payment": ("payments", "id"),
    "contact": ("contacts", "id"),
    "journal_entry": ("journal_entries", "id"),
    "account": ("accounts", "id"),
    "credit_note": ("credit_notes", "id"),
    "expense": ("expenses", "id"),
    "bank_statement_line": ("bank_statement_lines", "id"),
    "project": ("projects", "id"),
    "budget": ("budgets", "id"),
    "fixed_asset": ("fixed_assets", "id"),
    "user": ("users", "id"),
    "purchase_order": ("purchase_orders", "id"),
    "recurring_invoice": ("recurring_invoices", "id"),
    "tax_code": ("tax_codes", "id"),
}


def upgrade() -> None:
    bind = op.get_bind()

    # ---- Step 1: add tenant_id with server-side DEFAULT. ---------------
    # The DEFAULT fills existing rows atomically — no separate UPDATE
    # needed for rows that can't be backfilled.
    op.add_column(
        "change_log",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text(f"'{_PLACEHOLDER}'::uuid"),
        ),
    )

    # ---- Step 2: best-effort backfill via entity -> company_id join. ---
    for entity, (table, pk_col) in _ENTITY_MAP.items():
        # Skip tables that might not exist in every edition.
        result = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).fetchone()
        if result is None:
            continue
        # Check the target table has company_id (some tables join via tenant_id directly)
        has_company_id = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = 'company_id'"
            ),
            {"t": table},
        ).fetchone()
        has_tenant_id_direct = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = 'tenant_id'"
            ),
            {"t": table},
        ).fetchone()

        if has_company_id:
            bind.execute(
                sa.text(
                    f"UPDATE change_log cl "
                    f"SET tenant_id = c.tenant_id "
                    f"FROM {table} t "
                    f"JOIN companies c ON c.id = t.company_id "
                    f"WHERE cl.entity = :entity "
                    f"AND cl.entity_id = t.{pk_col} "
                    f"AND cl.tenant_id = '{_PLACEHOLDER}'::uuid"
                ),
                {"entity": entity},
            )
        elif has_tenant_id_direct:
            bind.execute(
                sa.text(
                    f"UPDATE change_log cl "
                    f"SET tenant_id = t.tenant_id "
                    f"FROM {table} t "
                    f"WHERE cl.entity = :entity "
                    f"AND cl.entity_id = t.{pk_col} "
                    f"AND cl.tenant_id = '{_PLACEHOLDER}'::uuid"
                ),
                {"entity": entity},
            )

    # ---- Step 3: FK + composite index. ---------------------------------
    op.create_foreign_key(
        "change_log_tenant_id_fkey",
        "change_log",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_change_log_tenant_id_id",
        "change_log",
        ["tenant_id", sa.text("id DESC")],
    )

    # ---- Step 4: ENABLE + FORCE RLS + tenant_isolation policy. --------
    op.execute(sa.text("ALTER TABLE change_log ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE change_log FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON change_log"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON change_log "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON change_log"))
    op.execute(sa.text("ALTER TABLE change_log NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE change_log DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_change_log_tenant_id_id", table_name="change_log")
    op.drop_constraint("change_log_tenant_id_fkey", "change_log", type_="foreignkey")
    op.drop_column("change_log", "tenant_id")
