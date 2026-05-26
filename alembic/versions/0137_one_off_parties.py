"""0137_one_off_parties — split one-off vendors and one-off customers into their own tables.

Rationale (Richard, 2026-05-26):
    "these contacts in one off should not be considered contacts in the
    sense they are now... I want to record the vendor, but this should
    technically not be a contact."

Architectural change:
    - New table `one_off_vendors` — lightweight party record for COD /
      walk-in / once-off suppliers. Carries enough to post an expense
      against a recognisable name without polluting the contact list.
    - New table `one_off_customers` — same shape, customer side.
    - Nullable `one_off_vendor_id` on expenses, bills, payments.
    - Nullable `one_off_customer_id` on invoices, credit_notes.
    - credit_notes.contact_id + payments.contact_id relaxed from
      NOT NULL → NULL because a one-off-pointed row has contact_id=NULL.
    - Either contact_id OR one_off_*_id may be set per transaction (not
      both); enforced by CHECK constraints.
    - Data migration: every `contacts WHERE is_one_off=true` row is
      moved to the appropriate one_off_* table (UUID preserved),
      transactions repointed, contact rows deleted, then
      `contacts.is_one_off` dropped.

Tenant-scoping checklist (see feedback_new-table-rls-checklist):
    [x] tenant_id NOT NULL column + FK to tenants(id)
    [x] ENABLE + FORCE ROW LEVEL SECURITY
    [x] CREATE POLICY tenant_isolation (USING + WITH CHECK)
    [x] Index on (tenant_id, last_used_at)
    [x] Service-layer filter (added in services/one_off_*.py)
    [x] Tenant-coherence trigger (one_off_*.tenant_id == company.tenant_id)
    [x] Cross-tenant probe test (added in tests/api/v1/test_one_off_*.py)

Revision ID: 0137_one_off_parties
Revises: 0136_api_tokens_rls
Create Date: 2026-05-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0137_one_off_parties"
down_revision: str | None = "0136_api_tokens_rls"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


VENDOR_PARENT_TABLES: tuple[str, ...] = ("expenses", "bills", "payments")
CUSTOMER_PARENT_TABLES: tuple[str, ...] = ("invoices", "credit_notes")
# Tables whose contact_id is currently NOT NULL — must be relaxed before
# repointing or the UPDATE that sets contact_id=NULL will fail.
RELAX_NULL_TABLES: tuple[str, ...] = ("credit_notes", "payments")


def _create_party_table(name: str, spend_or_billed: str) -> None:
    op.create_table(
        name,
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("abn", sa.String(14), nullable=True),
        sa.Column("default_account_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("default_tax_code", sa.String(16), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(spend_or_billed, sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_to_contact_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(f"ix_{name}_tenant_id", name, ["tenant_id"])
    op.create_index(f"ix_{name}_company_id", name, ["company_id"])
    op.create_index(f"ix_{name}_last_used", name, ["tenant_id", "last_used_at"])

    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {name}
        USING (tenant_id::text = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true))
    """)

    fn_name = f"{name}_tenant_coherence"
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {fn_name}()
        RETURNS trigger AS $$
        DECLARE
            co_tenant uuid;
        BEGIN
            SELECT tenant_id INTO co_tenant FROM companies WHERE id = NEW.company_id;
            IF co_tenant IS NULL THEN
                RAISE EXCEPTION '{name}.company_id (%) not found in companies', NEW.company_id;
            END IF;
            IF co_tenant <> NEW.tenant_id THEN
                RAISE EXCEPTION 'tenant_coherence: {name}.tenant_id (%) must equal companies.tenant_id (%) for company %',
                  NEW.tenant_id, co_tenant, NEW.company_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(f"""
        CREATE TRIGGER trg_{name}_tenant_coherence
        BEFORE INSERT OR UPDATE ON {name}
        FOR EACH ROW EXECUTE FUNCTION {fn_name}()
    """)


def _drop_party_table(name: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{name}_tenant_coherence ON {name}")
    op.execute(f"DROP FUNCTION IF EXISTS {name}_tenant_coherence()")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {name}")
    op.drop_index(f"ix_{name}_last_used", table_name=name)
    op.drop_index(f"ix_{name}_company_id", table_name=name)
    op.drop_index(f"ix_{name}_tenant_id", table_name=name)
    op.drop_table(name)


def upgrade() -> None:
    # 1. Create the two new party tables.
    _create_party_table("one_off_vendors", "total_spent")
    _create_party_table("one_off_customers", "total_billed")

    # 2. Relax NOT NULL on contact_id where it currently blocks the move.
    for tbl in RELAX_NULL_TABLES:
        op.alter_column(tbl, "contact_id", existing_type=pg.UUID(as_uuid=True), nullable=True)

    # 3. Add nullable FK columns to transactional tables.
    for tbl in VENDOR_PARENT_TABLES:
        op.add_column(
            tbl,
            sa.Column("one_off_vendor_id", pg.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"{tbl}_one_off_vendor_id_fkey", tbl, "one_off_vendors",
            ["one_off_vendor_id"], ["id"], ondelete="RESTRICT",
        )
        op.create_index(f"ix_{tbl}_one_off_vendor_id", tbl, ["one_off_vendor_id"])

    for tbl in CUSTOMER_PARENT_TABLES:
        op.add_column(
            tbl,
            sa.Column("one_off_customer_id", pg.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"{tbl}_one_off_customer_id_fkey", tbl, "one_off_customers",
            ["one_off_customer_id"], ["id"], ondelete="RESTRICT",
        )
        op.create_index(f"ix_{tbl}_one_off_customer_id", tbl, ["one_off_customer_id"])

    # 4. Mutex CHECK: at most one of (contact_id, one_off_*_id) is set per row.
    for tbl in VENDOR_PARENT_TABLES:
        op.create_check_constraint(
            f"ck_{tbl}_party_mutex", tbl,
            "(contact_id IS NULL OR one_off_vendor_id IS NULL)",
        )
    for tbl in CUSTOMER_PARENT_TABLES:
        op.create_check_constraint(
            f"ck_{tbl}_party_mutex", tbl,
            "(contact_id IS NULL OR one_off_customer_id IS NULL)",
        )

    # 5. Data migration. Notes prefix records origin for traceability.
    op.execute("""
        INSERT INTO one_off_vendors
            (id, tenant_id, company_id, name, abn, default_account_id,
             default_tax_code, notes, last_used_at, use_count, total_spent,
             archived_at, version, created_at, updated_at)
        SELECT
            c.id, c.tenant_id, c.company_id, c.name, c.abn, c.default_account_id,
            c.default_tax_code,
            CONCAT('[migrated from contact ', c.id::text, ' on 2026-05-26 mig=0137] ',
                   COALESCE(c.notes, '')),
            NULL, 0, 0,
            c.archived_at, 1, c.created_at, c.updated_at
        FROM contacts c
        WHERE c.is_one_off = true
          AND c.contact_type IN ('SUPPLIER', 'BOTH')
    """)

    op.execute("""
        INSERT INTO one_off_customers
            (id, tenant_id, company_id, name, abn, default_account_id,
             default_tax_code, notes, last_used_at, use_count, total_billed,
             archived_at, version, created_at, updated_at)
        SELECT
            c.id, c.tenant_id, c.company_id, c.name, c.abn, c.default_account_id,
            c.default_tax_code,
            CONCAT('[migrated from contact ', c.id::text, ' on 2026-05-26 mig=0137] ',
                   COALESCE(c.notes, '')),
            NULL, 0, 0,
            c.archived_at, 1, c.created_at, c.updated_at
        FROM contacts c
        WHERE c.is_one_off = true
          AND c.contact_type IN ('CUSTOMER', 'BOTH')
    """)

    # Repoint transactions. Single UPDATE per table; setting contact_id=NULL
    # in the same statement that sets one_off_*_id satisfies the new mutex CHECK.
    for tbl in VENDOR_PARENT_TABLES:
        op.execute(f"""
            UPDATE {tbl}
            SET one_off_vendor_id = contact_id, contact_id = NULL
            WHERE contact_id IN (SELECT id FROM one_off_vendors)
        """)

    for tbl in CUSTOMER_PARENT_TABLES:
        op.execute(f"""
            UPDATE {tbl}
            SET one_off_customer_id = contact_id, contact_id = NULL
            WHERE contact_id IN (SELECT id FROM one_off_customers)
        """)

    # 6. Backfill usage stats. Vendors counted from expenses+bills (payments
    # would double-count their parent). Date columns: expenses.expense_date,
    # bills.issue_date, invoices.issue_date, credit_notes.issue_date.
    op.execute("""
        UPDATE one_off_vendors v
        SET use_count = sub.cnt,
            last_used_at = sub.last_used,
            total_spent = sub.total
        FROM (
            SELECT vendor_id,
                   COUNT(*)::int AS cnt,
                   MAX(used_at)  AS last_used,
                   COALESCE(SUM(amount), 0) AS total
            FROM (
                SELECT one_off_vendor_id AS vendor_id, expense_date AS used_at, total AS amount
                FROM expenses WHERE one_off_vendor_id IS NOT NULL
                UNION ALL
                SELECT one_off_vendor_id, issue_date, total
                FROM bills WHERE one_off_vendor_id IS NOT NULL
            ) all_tx
            GROUP BY vendor_id
        ) sub
        WHERE v.id = sub.vendor_id
    """)

    op.execute("""
        UPDATE one_off_customers c
        SET use_count = sub.cnt,
            last_used_at = sub.last_used,
            total_billed = sub.total
        FROM (
            SELECT customer_id,
                   COUNT(*)::int AS cnt,
                   MAX(used_at)  AS last_used,
                   COALESCE(SUM(amount), 0) AS total
            FROM (
                SELECT one_off_customer_id AS customer_id, issue_date AS used_at, total AS amount
                FROM invoices WHERE one_off_customer_id IS NOT NULL
                UNION ALL
                SELECT one_off_customer_id, issue_date, total
                FROM credit_notes WHERE one_off_customer_id IS NOT NULL
            ) all_tx
            GROUP BY customer_id
        ) sub
        WHERE c.id = sub.customer_id
    """)

    # 7. Delete the contacts rows now that everything points elsewhere.
    op.execute("DELETE FROM contacts WHERE is_one_off = true")

    # 8. Drop legacy bucket views that depend on contacts.is_one_off.
    # These exist only on sauer (created out-of-band, not in any migration);
    # IF EXISTS makes the statement a no-op on the other stacks.
    op.execute("DROP VIEW IF EXISTS v_suppliers_main")
    op.execute("DROP VIEW IF EXISTS v_customers_main")
    op.execute("DROP VIEW IF EXISTS v_one_off_suppliers")
    op.execute("DROP VIEW IF EXISTS v_one_off_customers")
    op.execute("DROP VIEW IF EXISTS v_beneficiaries")

    # 9. Drop is_one_off — no longer meaningful.
    op.drop_column("contacts", "is_one_off")


def downgrade() -> None:
    # Re-add the column, restore rows from the new tables, repoint
    # transactions, drop the new tables. Caveat: usage stats (use_count,
    # last_used_at, total_*) are lost — they were derived, not authoritative.
    op.add_column(
        "contacts",
        sa.Column("is_one_off", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.execute("""
        INSERT INTO contacts
            (id, company_id, tenant_id, name, contact_type, abn,
             default_account_id, default_tax_code, notes, archived_at,
             version, created_at, updated_at, is_one_off)
        SELECT
            v.id, v.company_id, v.tenant_id, v.name, 'SUPPLIER'::contact_type_enum,
            v.abn, v.default_account_id, v.default_tax_code, v.notes,
            v.archived_at, 1, v.created_at, v.updated_at, true
        FROM one_off_vendors v
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO contacts
            (id, company_id, tenant_id, name, contact_type, abn,
             default_account_id, default_tax_code, notes, archived_at,
             version, created_at, updated_at, is_one_off)
        SELECT
            c.id, c.company_id, c.tenant_id, c.name, 'CUSTOMER'::contact_type_enum,
            c.abn, c.default_account_id, c.default_tax_code, c.notes,
            c.archived_at, 1, c.created_at, c.updated_at, true
        FROM one_off_customers c
        ON CONFLICT (id) DO NOTHING
    """)

    for tbl in VENDOR_PARENT_TABLES:
        op.execute(f"UPDATE {tbl} SET contact_id = one_off_vendor_id, one_off_vendor_id = NULL WHERE one_off_vendor_id IS NOT NULL")
    for tbl in CUSTOMER_PARENT_TABLES:
        op.execute(f"UPDATE {tbl} SET contact_id = one_off_customer_id, one_off_customer_id = NULL WHERE one_off_customer_id IS NOT NULL")

    for tbl in VENDOR_PARENT_TABLES:
        op.drop_constraint(f"ck_{tbl}_party_mutex", tbl, type_="check")
        op.drop_index(f"ix_{tbl}_one_off_vendor_id", table_name=tbl)
        op.drop_constraint(f"{tbl}_one_off_vendor_id_fkey", tbl, type_="foreignkey")
        op.drop_column(tbl, "one_off_vendor_id")
    for tbl in CUSTOMER_PARENT_TABLES:
        op.drop_constraint(f"ck_{tbl}_party_mutex", tbl, type_="check")
        op.drop_index(f"ix_{tbl}_one_off_customer_id", table_name=tbl)
        op.drop_constraint(f"{tbl}_one_off_customer_id_fkey", tbl, type_="foreignkey")
        op.drop_column(tbl, "one_off_customer_id")

    # Restore NOT NULL on credit_notes.contact_id + payments.contact_id.
    for tbl in RELAX_NULL_TABLES:
        op.alter_column(tbl, "contact_id", existing_type=pg.UUID(as_uuid=True), nullable=False)

    _drop_party_table("one_off_customers")
    _drop_party_table("one_off_vendors")
