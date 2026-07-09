"""0137_tpar — TPAR (Taxable Payments Annual Report) tables.

Australian SMBs in construction, cleaning, courier, IT, road freight,
and security industries must lodge a TPAR each financial year listing
payments made to reportable contractors. This migration adds the
tpar_runs + tpar_lines tables that the aggregator (services/tpar.py)
populates from bills + expenses paid to ``is_tpar_supplier=true``
contacts in the FY.

Both tables are tenant-scoped via FORCE RLS + tenant_isolation policy
(same shape as other transactional tables).

Revision ID: 0137_tpar
Revises: 0136_api_tokens_rls
Create Date: 2026-05-27
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0138_tpar"
down_revision: str | None = "0137_one_off_parties"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE tpar_run_status_enum AS ENUM ('DRAFT','FINALISED','LODGED','VOIDED');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tpar_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            fy_start date NOT NULL,
            fy_end   date NOT NULL,
            status tpar_run_status_enum NOT NULL DEFAULT 'DRAFT',
            generated_at timestamptz NOT NULL DEFAULT now(),
            finalised_at timestamptz,
            finalised_by varchar,
            lodged_at timestamptz,
            lodged_reference varchar(64),
            total_payee_count integer NOT NULL DEFAULT 0,
            total_gross_amount numeric(18,2) NOT NULL DEFAULT 0,
            total_gst_amount numeric(18,2) NOT NULL DEFAULT 0,
            notes text,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz,
            CONSTRAINT uq_tpar_runs_company_fy UNIQUE (company_id, fy_start)
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_tpar_runs_company_status ON tpar_runs (company_id, status);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tpar_runs_tenant ON tpar_runs (tenant_id);")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tpar_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tpar_run_id uuid NOT NULL REFERENCES tpar_runs(id) ON DELETE CASCADE,
            contact_id uuid NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            payee_name varchar(255) NOT NULL,
            payee_abn varchar(20),
            payee_address_line1 varchar(255),
            payee_address_line2 varchar(255),
            payee_city varchar(128),
            payee_state varchar(8),
            payee_postcode varchar(8),
            payee_country varchar(64),
            gross_paid numeric(18,2) NOT NULL DEFAULT 0,
            gst_paid numeric(18,2) NOT NULL DEFAULT 0,
            bill_count integer NOT NULL DEFAULT 0,
            expense_count integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_tpar_lines_run_contact UNIQUE (tpar_run_id, contact_id)
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_tpar_lines_run ON tpar_lines (tpar_run_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tpar_lines_tenant ON tpar_lines (tenant_id);")

    for tbl in ("tpar_runs", "tpar_lines"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            DO $$ BEGIN
                CREATE POLICY tenant_isolation ON {tbl}
                    USING (tenant_id::text = current_setting('app.current_tenant', true))
                    WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true));
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
            """
        )

    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON tpar_runs, tpar_lines TO saebooks_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tpar_lines CASCADE;")
    op.execute("DROP TABLE IF EXISTS tpar_runs CASCADE;")
    op.execute("DROP TYPE IF EXISTS tpar_run_status_enum;")
