"""Add quotes tables + invoices.source_quote_id for quote→invoice audit trail.

0095_quotes_tables branched off 0094_purchase_orders in the public repo but some
environments are ahead (DB at 0096_launch_promo via a missing 0095_sync_state_tables
stub). This migration chains off 0096_launch_promo and idempotently creates the
quotes tables (CREATE TABLE IF NOT EXISTS) before adding source_quote_id to invoices.

Revision ID: 0097_invoices_source_quote_id
Revises: 0096_launch_promo
Create Date: 2026-05-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0097_invoices_source_quote_id"
down_revision: str | None = "0096_launch_promo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUOTE_STATUSES = (
    "DRAFT",
    "SENT",
    "ACCEPTED",
    "DECLINED",
    "ARCHIVED",
    "INVOICED",
)
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    bind = op.get_bind()

    # 1. quote_status_enum type (idempotent)
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='quote_status_enum') THEN "
            "    CREATE TYPE quote_status_enum AS ENUM "
            "      ('DRAFT','SENT','ACCEPTED','DECLINED','ARCHIVED','INVOICED'); "
            "  END IF; "
            "END $$"
        )
    )

    # 2. quotes table (idempotent)
    bind.execute(
        sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS quotes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT
                    DEFAULT '{_DEFAULT_TENANT}'::uuid,
                company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                number VARCHAR(32),
                customer_id UUID NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
                status quote_status_enum NOT NULL DEFAULT 'DRAFT',
                issue_date DATE NOT NULL,
                expiry_date DATE,
                currency VARCHAR(3) NOT NULL DEFAULT 'AUD',
                subtotal NUMERIC(18,2) NOT NULL DEFAULT 0,
                tax_total NUMERIC(18,2) NOT NULL DEFAULT 0,
                total NUMERIC(18,2) NOT NULL DEFAULT 0,
                validity_days INTEGER NOT NULL DEFAULT 28,
                deposit_pct NUMERIC(6,2) NOT NULL DEFAULT 50,
                late_fee_pct_per_month NUMERIC(6,4) NOT NULL DEFAULT 2.5,
                is_supply_only BOOLEAN NOT NULL DEFAULT false,
                notes TEXT,
                terms TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                accepted_at TIMESTAMPTZ,
                declined_at TIMESTAMPTZ,
                invoiced_at TIMESTAMPTZ,
                invoice_id UUID REFERENCES invoices(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )

    # 3. Unique constraint on quotes (idempotent)
    bind.execute(
        sa.text(
            "ALTER TABLE quotes "
            "ADD CONSTRAINT uq_quotes_tenant_number UNIQUE (tenant_id, number) "
            "NOT VALID"
        ).execution_options(autocommit=False)
        if False  # skip — using DO block below
        else sa.text(
            "DO $$ BEGIN "
            "  IF NOT EXISTS ("
            "    SELECT 1 FROM pg_constraint WHERE conname='uq_quotes_tenant_number'"
            "  ) THEN "
            "    ALTER TABLE quotes "
            "    ADD CONSTRAINT uq_quotes_tenant_number UNIQUE (tenant_id, number); "
            "  END IF; "
            "END $$"
        )
    )

    # 4. Indexes on quotes (idempotent via CREATE INDEX IF NOT EXISTS)
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_quotes_tenant_customer_status "
            "ON quotes (tenant_id, customer_id, status)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_quotes_tenant_status_expiry "
            "ON quotes (tenant_id, status, expiry_date)"
        )
    )

    # 5. RLS on quotes (idempotent)
    bind.execute(sa.text("ALTER TABLE quotes ENABLE ROW LEVEL SECURITY"))
    bind.execute(sa.text("ALTER TABLE quotes FORCE ROW LEVEL SECURITY"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "  IF NOT EXISTS ("
            "    SELECT 1 FROM pg_policies WHERE tablename='quotes' AND policyname='tenant_isolation'"
            "  ) THEN "
            f"    CREATE POLICY tenant_isolation ON quotes "
            f"    FOR ALL USING {_USING} WITH CHECK {_USING}; "
            "  END IF; "
            "END $$"
        )
    )

    # 6. quote_lines table (idempotent)
    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS quote_lines (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                quote_id UUID NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
                line_no INTEGER NOT NULL,
                description TEXT NOT NULL,
                quantity NUMERIC(18,4) NOT NULL DEFAULT 1,
                unit_price NUMERIC(18,4) NOT NULL DEFAULT 0,
                tax_code_id UUID REFERENCES tax_codes(id) ON DELETE SET NULL,
                line_total NUMERIC(18,2) NOT NULL DEFAULT 0,
                account_id UUID REFERENCES accounts(id) ON DELETE SET NULL
            )
            """
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_quote_lines_quote "
            "ON quote_lines (quote_id)"
        )
    )

    # 7. source_quote_id on invoices (idempotent)
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "  IF NOT EXISTS ("
            "    SELECT 1 FROM information_schema.columns "
            "    WHERE table_name='invoices' AND column_name='source_quote_id'"
            "  ) THEN "
            "    ALTER TABLE invoices "
            "    ADD COLUMN source_quote_id UUID REFERENCES quotes(id) ON DELETE SET NULL; "
            "  END IF; "
            "END $$"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_invoices_source_quote_id "
            "ON invoices (source_quote_id)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_invoices_source_quote_id"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "  IF EXISTS ("
            "    SELECT 1 FROM information_schema.columns "
            "    WHERE table_name='invoices' AND column_name='source_quote_id'"
            "  ) THEN "
            "    ALTER TABLE invoices DROP COLUMN source_quote_id; "
            "  END IF; "
            "END $$"
        )
    )
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_quote_lines_quote"))
    bind.execute(sa.text("DROP TABLE IF EXISTS quote_lines"))
    bind.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON quotes"))
    bind.execute(sa.text("ALTER TABLE quotes NO FORCE ROW LEVEL SECURITY"))
    bind.execute(sa.text("ALTER TABLE quotes DISABLE ROW LEVEL SECURITY"))
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_quotes_tenant_status_expiry"))
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_quotes_tenant_customer_status"))
    bind.execute(sa.text("DROP TABLE IF EXISTS quotes"))
    bind.execute(sa.text("DROP TYPE IF EXISTS quote_status_enum"))
