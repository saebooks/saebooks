"""0141_super_lodgements — Payday Super Phase 1 lodgement tracking.

From 1 July 2026 every SG payment must reach the employee's fund within
seven days of payday. Phase 1 of the Payday Super work tracks each
lodgement as a first-class entity, generates the SAFF v1 CSV the
clearing-house portal accepts, and snapshots employee + fund details so
member-data changes after the fact don't rewrite the historical record.

This migration adds:

* ``super_lodgement_runs`` — one row per pay-run that has super due.
  Status lifecycle: DRAFT → FINALISED → SUBMITTED → ACCEPTED / FAILED,
  with VOIDED for runs cancelled mid-flight.
* ``super_lodgement_lines`` — one row per (run, employee, fund), with a
  full snapshot of employee + fund details captured at lodgement time.

Both tables are tenant-scoped via FORCE RLS + tenant_isolation policy
(same shape as tpar_runs / tpar_lines).

Phase 2 will wire in actual clearing-house API submission + ACK polling;
Phase 1 is manual-upload + manual-mark-submitted via the API.

Revision ID: 0141_super_lodgements
Revises: 0140_payments_one_off_customer
Create Date: 2026-05-30
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0141_super_lodgements"
down_revision: str | None = "0140_payments_one_off_customer"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE super_lodgement_status_enum AS ENUM (
                'DRAFT','FINALISED','SUBMITTED','ACCEPTED','FAILED','VOIDED'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS super_lodgement_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            tenant_id  uuid NOT NULL REFERENCES tenants(id)   ON DELETE RESTRICT,
            pay_run_id uuid NOT NULL REFERENCES pay_runs(id)  ON DELETE RESTRICT,
            period_start date NOT NULL,
            period_end   date NOT NULL,
            payment_date date NOT NULL,
            status super_lodgement_status_enum NOT NULL DEFAULT 'DRAFT',
            generated_at timestamptz NOT NULL DEFAULT now(),
            finalised_at timestamptz,
            finalised_by varchar,
            submitted_at timestamptz,
            submitted_reference varchar(128),
            total_employee_count integer NOT NULL DEFAULT 0,
            total_amount numeric(18,2) NOT NULL DEFAULT 0,
            notes text,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz,
            CONSTRAINT uq_super_lodgement_runs_company_pay_run
                UNIQUE (company_id, pay_run_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_lodgement_runs_company_status "
        "ON super_lodgement_runs (company_id, status);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_lodgement_runs_tenant "
        "ON super_lodgement_runs (tenant_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_lodgement_runs_pay_run "
        "ON super_lodgement_runs (pay_run_id);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS super_lodgement_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            super_lodgement_run_id uuid NOT NULL
                REFERENCES super_lodgement_runs(id) ON DELETE CASCADE,
            employee_id   uuid NOT NULL REFERENCES employees(id)   ON DELETE RESTRICT,
            super_fund_id uuid          REFERENCES super_funds(id) ON DELETE RESTRICT,
            tenant_id     uuid NOT NULL REFERENCES tenants(id)     ON DELETE RESTRICT,

            -- Snapshot of employee + fund at lodgement time. Member fund
            -- details can change between pay date and lodgement; the SAFF
            -- must reflect state at the time the contribution was paid.
            employee_first_name varchar(128),
            employee_last_name  varchar(128) NOT NULL,
            employee_tfn_status varchar(32),
            employee_address_line1 varchar(255),
            employee_address_line2 varchar(255),
            employee_suburb varchar(128),
            employee_state  varchar(8),
            employee_postcode varchar(8),
            employee_email  varchar(128),
            fund_name varchar(128),
            fund_usi  varchar(11),
            fund_spin varchar(20),
            fund_is_smsf boolean NOT NULL DEFAULT false,
            fund_employer_abn varchar(14),
            fund_esa  varchar(16),
            member_number varchar(64),

            gross_payment           numeric(18,2) NOT NULL DEFAULT 0,
            sg_amount               numeric(18,2) NOT NULL DEFAULT 0,
            salary_sacrifice_amount numeric(18,2) NOT NULL DEFAULT 0,
            additional_amount       numeric(18,2) NOT NULL DEFAULT 0,
            total_amount            numeric(18,2) NOT NULL DEFAULT 0,

            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_super_lodgement_lines_run_emp_fund
                UNIQUE (super_lodgement_run_id, employee_id, super_fund_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_lodgement_lines_run "
        "ON super_lodgement_lines (super_lodgement_run_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_lodgement_lines_employee "
        "ON super_lodgement_lines (employee_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_super_lodgement_lines_tenant "
        "ON super_lodgement_lines (tenant_id);"
    )

    for tbl in ("super_lodgement_runs", "super_lodgement_lines"):
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
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON super_lodgement_runs, super_lodgement_lines
                    TO saebooks_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS super_lodgement_lines CASCADE;")
    op.execute("DROP TABLE IF EXISTS super_lodgement_runs CASCADE;")
    op.execute("DROP TYPE IF EXISTS super_lodgement_status_enum;")
