"""Company structural guards on journal tables.

Adds ``journal_lines.company_id`` (deterministically backfilled from the
parent ``journal_entries.company_id`` — a SAFE structural backfill required to
make the column NOT NULL, NOT a claim of historical provenance), a composite
``(account_id, company_id)`` FK so a line can never reference a sister
company's account, a BEFORE INSERT/UPDATE trigger that auto-fills + pins
``journal_lines.company_id`` to its parent entry, and a 0131-style
tenant<->company coherence trigger on ``journal_entries`` (which migration 0131
omitted for this table).

These guards are always-on (no GUC dependency) and close the cross-company
hole structurally for the journal tables.

Revision ID: 0152_journal_company_structural
Revises:     0151_stmt_templates
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0152_journal_company_structural"
down_revision: str | None = "0151_stmt_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. accounts needs a UNIQUE(id, company_id) to be the composite-FK target.
    #    id is already the PK (unique on its own); this composite unique exists
    #    solely so a FOREIGN KEY can reference (id, company_id). Idempotent.
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_accounts_id_company'
                      AND conrelid = 'accounts'::regclass
                ) THEN
                    ALTER TABLE accounts
                        ADD CONSTRAINT uq_accounts_id_company UNIQUE (id, company_id);
                END IF;
            END $$;
            """
        )
    )

    # 2. journal_lines.company_id — add nullable, backfill from the parent
    #    entry, then enforce NOT NULL. entry_id is itself NOT NULL with a FK to
    #    journal_entries, so every line has exactly one parent and the backfill
    #    covers 100% of rows (no orphans -> no residual NULLs).
    op.add_column(
        "journal_lines",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE journal_lines jl SET company_id = je.company_id "
            "FROM journal_entries je WHERE je.id = jl.entry_id"
        )
    )
    op.alter_column("journal_lines", "company_id", nullable=False)
    op.create_foreign_key(
        "fk_journal_lines_company",
        "journal_lines",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_journal_lines_company_id", "journal_lines", ["company_id"]
    )

    # 3. Composite FK: a line's (account_id, company_id) must exist in accounts.
    #    Drop the single-column account FK first, then add the composite one.
    op.drop_constraint(
        "journal_lines_account_id_fkey", "journal_lines", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_journal_lines_account_company",
        "journal_lines",
        "accounts",
        ["account_id", "company_id"],
        ["id", "company_id"],
        ondelete="RESTRICT",
    )

    # 4. Parent-coherence trigger on journal_lines: auto-fill company_id from
    #    the parent entry when NULL (so existing line-creation sites that do
    #    not set it keep working), else assert it matches the parent.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION assert_journal_line_company()
            RETURNS trigger AS $func$
            DECLARE v_parent_company uuid;
            BEGIN
                SELECT company_id INTO v_parent_company
                    FROM journal_entries WHERE id = NEW.entry_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'journal_line_company: parent entry % not found',
                        NEW.entry_id;
                END IF;
                IF NEW.company_id IS NULL THEN
                    NEW.company_id := v_parent_company;
                ELSIF NEW.company_id IS DISTINCT FROM v_parent_company THEN
                    RAISE EXCEPTION
                        'journal_line_company: line company % must equal parent entry company %',
                        NEW.company_id, v_parent_company;
                END IF;
                RETURN NEW;
            END;
            $func$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_journal_lines_company ON journal_lines"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_journal_lines_company "
            "BEFORE INSERT OR UPDATE ON journal_lines "
            "FOR EACH ROW EXECUTE FUNCTION assert_journal_line_company()"
        )
    )

    # 5. Extend the existing 0131 tenant<->company coherence to journal_entries.
    #    CREATE OR REPLACE keeps 0131's function definition (the 8 existing
    #    triggers depend on it); we only attach a new trigger for this table.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION assert_child_tenant_matches_company()
            RETURNS trigger AS $func$
            DECLARE v_company_tenant_id uuid;
            BEGIN
                IF NEW.company_id IS NULL THEN
                    RAISE EXCEPTION
                        'tenant_coherence: %.company_id must not be NULL',
                        TG_TABLE_NAME;
                END IF;
                SELECT tenant_id INTO v_company_tenant_id
                    FROM companies WHERE id = NEW.company_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'tenant_coherence: company % not found (table %)',
                        NEW.company_id, TG_TABLE_NAME;
                END IF;
                IF NEW.tenant_id IS DISTINCT FROM v_company_tenant_id THEN
                    RAISE EXCEPTION
                        'tenant_coherence: %.tenant_id (%) must equal companies.tenant_id (%) for company %',
                        TG_TABLE_NAME, NEW.tenant_id, v_company_tenant_id, NEW.company_id;
                END IF;
                RETURN NEW;
            END;
            $func$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_journal_entries_tenant_coherence "
            "ON journal_entries"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_journal_entries_tenant_coherence "
            "BEFORE INSERT OR UPDATE ON journal_entries "
            "FOR EACH ROW EXECUTE FUNCTION assert_child_tenant_matches_company()"
        )
    )

    # 6. Explicit GRANT for the runtime app role — default privileges miss
    #    tables/columns altered under the non-owner migration role.
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON journal_lines TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_journal_entries_tenant_coherence "
            "ON journal_entries"
        )
    )
    # NOTE: do NOT drop assert_child_tenant_matches_company() — 0131's eight
    # triggers depend on it.
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_journal_lines_company ON journal_lines"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS assert_journal_line_company()"))
    op.drop_constraint(
        "fk_journal_lines_account_company", "journal_lines", type_="foreignkey"
    )
    op.create_foreign_key(
        "journal_lines_account_id_fkey",
        "journal_lines",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index("ix_journal_lines_company_id", table_name="journal_lines")
    op.drop_constraint(
        "fk_journal_lines_company", "journal_lines", type_="foreignkey"
    )
    op.drop_column("journal_lines", "company_id")
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_accounts_id_company'
                      AND conrelid = 'accounts'::regclass
                ) THEN
                    ALTER TABLE accounts DROP CONSTRAINT uq_accounts_id_company;
                END IF;
            END $$;
            """
        )
    )
