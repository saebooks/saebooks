"""0134_branches — branches table + branch_id FK on transactional tables.

A branch is a sub-divisional tag on transactions, scoped per Company.
Useful for tracking SAE Engineering vs other trading-name activity inside
Saueesti Trust's books, or multi-location reporting within a single legal
entity. NOT a legal entity — that's what Company is for.

Schema:
  branches(id, company_id, tenant_id, code, name, is_default, archived_at,
           version, created_at) — tenant-scoped, FORCE RLS + tenant_isolation
                                  policy + tenant-coherence trigger.

branch_id added (nullable) to: journal_entries, invoices, bills, bsls,
                                payments, credit_notes, expenses.

Nullable so existing data stays valid until backfilled by ops code.
A separate coherence trigger asserts that when set, branch.company_id
matches the parent record's company_id.

Revision ID: 0134_branches
Revises: 0133_entity_types
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0134_branches"
down_revision: str | None = "0133_entity_types"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


BRANCH_PARENT_TABLES: tuple[str, ...] = (
    "journal_entries",
    "invoices",
    "bills",
    "bank_statement_lines",
    "payments",
    "credit_notes",
    "expenses",
)


def upgrade() -> None:
    # 1. branches table
    op.create_table(
        "branches",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "code", name="uq_branches_company_code"),
    )
    op.create_index("ix_branches_company_id", "branches", ["company_id"])
    op.create_index("ix_branches_tenant_id", "branches", ["tenant_id"])

    # Partial unique index — at most one is_default=true per (company_id,
    # not-archived). Implemented as a partial unique on company_id where
    # is_default=true AND archived_at IS NULL.
    op.execute("""
        CREATE UNIQUE INDEX uq_branches_one_default_per_company
        ON branches (company_id)
        WHERE is_default = true AND archived_at IS NULL
    """)

    # RLS
    op.execute("ALTER TABLE branches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE branches FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON branches
        USING (tenant_id::text = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true))
    """)

    # Tenant coherence trigger (branches.tenant_id == companies.tenant_id)
    op.execute("""
        CREATE OR REPLACE FUNCTION branches_tenant_coherence()
        RETURNS trigger AS $$
        DECLARE
            co_tenant uuid;
        BEGIN
            SELECT tenant_id INTO co_tenant FROM companies WHERE id = NEW.company_id;
            IF co_tenant IS NULL THEN
                RAISE EXCEPTION 'branches.company_id (%) not found in companies', NEW.company_id;
            END IF;
            IF co_tenant <> NEW.tenant_id THEN
                RAISE EXCEPTION 'tenant_coherence: branches.tenant_id (%) must equal companies.tenant_id (%) for company %',
                  NEW.tenant_id, co_tenant, NEW.company_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_branches_tenant_coherence
        BEFORE INSERT OR UPDATE ON branches
        FOR EACH ROW EXECUTE FUNCTION branches_tenant_coherence()
    """)

    # 2. branch_id FK on each transactional table (nullable)
    for tbl in BRANCH_PARENT_TABLES:
        op.add_column(
            tbl,
            sa.Column("branch_id", pg.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"{tbl}_branch_id_fkey",
            tbl,
            "branches",
            ["branch_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{tbl}_branch_id", tbl, ["branch_id"])

    # 3. Coherence trigger per parent table: branch.company_id must equal
    # parent.company_id when branch_id is set. Use a single shared function;
    # one trigger per parent table.
    op.execute("""
        CREATE OR REPLACE FUNCTION branch_company_coherence()
        RETURNS trigger AS $$
        DECLARE
            br_company uuid;
        BEGIN
            IF NEW.branch_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT company_id INTO br_company FROM branches WHERE id = NEW.branch_id;
            IF br_company IS NULL THEN
                RAISE EXCEPTION 'branch % not found', NEW.branch_id;
            END IF;
            IF br_company <> NEW.company_id THEN
                RAISE EXCEPTION 'branch_company_coherence: branch.company_id (%) must equal %.company_id (%) on row',
                  br_company, TG_TABLE_NAME, NEW.company_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    for tbl in BRANCH_PARENT_TABLES:
        op.execute(f"""
            CREATE TRIGGER trg_{tbl}_branch_company_coherence
            BEFORE INSERT OR UPDATE OF branch_id, company_id ON {tbl}
            FOR EACH ROW EXECUTE FUNCTION branch_company_coherence()
        """)


def downgrade() -> None:
    for tbl in BRANCH_PARENT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_branch_company_coherence ON {tbl}")
        op.drop_index(f"ix_{tbl}_branch_id", table_name=tbl)
        op.drop_constraint(f"{tbl}_branch_id_fkey", tbl, type_="foreignkey")
        op.drop_column(tbl, "branch_id")
    op.execute("DROP FUNCTION IF EXISTS branch_company_coherence()")
    op.execute("DROP TRIGGER IF EXISTS trg_branches_tenant_coherence ON branches")
    op.execute("DROP FUNCTION IF EXISTS branches_tenant_coherence()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON branches")
    op.drop_index("uq_branches_one_default_per_company", table_name="branches")
    op.drop_index("ix_branches_tenant_id", table_name="branches")
    op.drop_index("ix_branches_company_id", table_name="branches")
    op.drop_table("branches")
