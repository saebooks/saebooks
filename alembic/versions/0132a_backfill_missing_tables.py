"""0132a_backfill_missing_tables — repair migration chain for fresh installs.

The expenses, expense_lines, and extracted_doc_review tables were created
by migrations 0105-0108 on a feature branch (feat/cashbook-persistence)
that never landed on main. The canonical chain therefore has 0133+ that
references these tables without creating them — fresh installs fail at
0134_branches when it tries to ALTER expenses ADD COLUMN branch_id.

This migration creates the three missing tables as a backfill, slotted
between 0132 and 0133. It is fully idempotent (CREATE TABLE IF NOT
EXISTS + DO blocks for enum/policy/RLS) so existing prod DBs (primary,
acme, app-preview, cashbook-demo) which already have these tables
from the dropped branch will see this as a no-op.

The branch_id column on expenses is INTENTIONALLY NOT added here —
0134_branches adds it as part of its multi-table branch_id rollout.

Revision ID: 0132a_backfill_missing_tables
Revises: 0132_gst_system_managed_backfill
Create Date: 2026-05-26
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0132a_backfill_missing_tables"
down_revision: str | None = "0132_gst_system_managed_backfill"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # expense_status_enum — create only if absent
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE expense_status_enum AS ENUM ('DRAFT','POSTED','VOIDED');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # expenses (without branch_id — 0134 adds that)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            contact_id uuid REFERENCES contacts(id) ON DELETE RESTRICT,
            payment_account_id uuid NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            number varchar(32),
            reference varchar(64),
            expense_date date NOT NULL,
            status expense_status_enum NOT NULL DEFAULT 'DRAFT',
            subtotal numeric(18,2) NOT NULL DEFAULT 0,
            tax_total numeric(18,2) NOT NULL DEFAULT 0,
            total numeric(18,2) NOT NULL DEFAULT 0,
            currency varchar(3) NOT NULL DEFAULT 'AUD',
            fx_rate numeric(18,8) NOT NULL DEFAULT 1,
            base_subtotal numeric(18,2) NOT NULL DEFAULT 0,
            base_tax_total numeric(18,2) NOT NULL DEFAULT 0,
            base_total numeric(18,2) NOT NULL DEFAULT 0,
            notes text,
            posted_at timestamptz,
            posted_by varchar,
            journal_entry_id uuid REFERENCES journal_entries(id) ON DELETE SET NULL,
            void_journal_entry_id uuid REFERENCES journal_entries(id) ON DELETE SET NULL,
            external_id varchar(255),
            external_source varchar(64),
            external_etag varchar(255),
            external_payload jsonb,
            version integer NOT NULL DEFAULT 1,
            tenant_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'::uuid
                REFERENCES tenants(id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz,
            CONSTRAINT uq_expenses_company_number UNIQUE (company_id, number)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_expenses_company_contact "
        "ON expenses (company_id, contact_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_expenses_company_date "
        "ON expenses (company_id, expense_date);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_expenses_company_status "
        "ON expenses (company_id, status);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_expenses_payment_account "
        "ON expenses (company_id, payment_account_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_expenses_external "
        "ON expenses (company_id, external_source, external_id) "
        "WHERE external_id IS NOT NULL;"
    )

    # expense_lines
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS expense_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            expense_id uuid NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
            line_no integer NOT NULL,
            description text NOT NULL,
            account_id uuid NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            tax_code_id uuid REFERENCES tax_codes(id) ON DELETE SET NULL,
            quantity numeric(18,4) NOT NULL DEFAULT 1,
            unit_price numeric(18,4) NOT NULL DEFAULT 0,
            discount_pct numeric(6,2) NOT NULL DEFAULT 0,
            line_subtotal numeric(18,2) NOT NULL DEFAULT 0,
            line_tax numeric(18,2) NOT NULL DEFAULT 0,
            line_total numeric(18,2) NOT NULL DEFAULT 0,
            project_id uuid REFERENCES projects(id) ON DELETE SET NULL
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_expense_lines_expense "
        "ON expense_lines (expense_id);"
    )

    # extracted_doc_review (paperless-AI extraction queue)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS extracted_doc_review (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            source_je_id uuid REFERENCES journal_entries(id) ON DELETE CASCADE,
            paperless_doc_id integer NOT NULL,
            paperless_doc_title text,
            paperless_doc_url text,
            saebooks_kind text NOT NULL CHECK (saebooks_kind IN ('bill','expense')),
            saebooks_ref text NOT NULL,
            saebooks_number text,
            saebooks_total numeric(14,2),
            saebooks_vendor_name text,
            saebooks_date date,
            extracted_vendor_name text,
            extracted_vendor_abn text,
            extracted_invoice_number text,
            extracted_invoice_date date,
            extracted_subtotal_ex_gst numeric(14,2),
            extracted_gst_total numeric(14,2),
            extracted_total_inc_gst numeric(14,2),
            n_line_items integer,
            extracted_confidence text CHECK (extracted_confidence IN ('high','medium','low')),
            extraction_notes text,
            extraction jsonb NOT NULL,
            extraction_model text NOT NULL,
            extracted_at timestamptz NOT NULL DEFAULT now(),
            totals_match boolean GENERATED ALWAYS AS (
                extracted_total_inc_gst IS NOT NULL
                AND saebooks_total IS NOT NULL
                AND abs(extracted_total_inc_gst - saebooks_total) < 1.0
            ) STORED,
            review_status text NOT NULL DEFAULT 'PENDING'
                CHECK (review_status IN ('PENDING','APPROVED','REJECTED','PROMOTED','PHASE1_MISMATCH')),
            reviewed_by text,
            reviewed_at timestamptz,
            review_notes text,
            promoted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT extracted_doc_review_tenant_paperless_unique
                UNIQUE (tenant_id, paperless_doc_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extracted_doc_review_company_status "
        "ON extracted_doc_review (company_id, review_status);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extracted_doc_review_je "
        "ON extracted_doc_review (source_je_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extracted_doc_review_tenant "
        "ON extracted_doc_review (tenant_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extracted_doc_review_totals_match "
        "ON extracted_doc_review (totals_match) WHERE review_status = 'PENDING';"
    )

    # RLS — idempotent via DO blocks
    for tbl in ("expenses", "extracted_doc_review"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            DO $$ BEGIN
                CREATE POLICY tenant_isolation ON {tbl}
                    USING (tenant_id::text = current_setting('app.current_tenant', true))
                    WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true));
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
            """
        )

    # ACLs for saebooks_app role (created by 0128). Skip silently if role absent.
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON
                    expenses, expense_lines, extracted_doc_review
                TO saebooks_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Best-effort. Downgrading past this migration on an existing DB will
    # only succeed if no rows reference these tables.
    op.execute("DROP TABLE IF EXISTS extracted_doc_review CASCADE;")
    op.execute("DROP TABLE IF EXISTS expense_lines CASCADE;")
    op.execute("DROP TABLE IF EXISTS expenses CASCADE;")
    op.execute("DROP TYPE IF EXISTS expense_status_enum;")
