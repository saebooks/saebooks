"""Trigger: assert child.tenant_id = parent.tenant_id on denormalised tables.

Lane 4 P0-2 (Agent H) — belt-and-braces against data corruption where a row's
tenant_id disagrees with the tenant_id of its owning company.  Such a
mismatch is the root cause of the accounts cross-tenant leak found in the
critic seeding: the row carried ``company_id = tenant-B's company`` but
``tenant_id = tenant-A``.

The application now adds ``tenant_id`` predicates to every list/get query
on the affected service functions (accounts, contacts, items, tax_codes,
projects, journal_templates, departments, cost_centres).  This migration
adds a BEFORE INSERT OR UPDATE trigger on all eight tables that asserts::

    NEW.tenant_id = (SELECT tenant_id FROM companies WHERE id = NEW.company_id)

If the assertion fails the trigger raises an exception and aborts the
statement — a corrupted row can never be stored in the first place.

Behaviour
---------
* Fires BEFORE INSERT OR UPDATE on each of the eight tables.
* Performs a single-row SELECT against ``companies`` using the FK index;
  the lookup is fast (PK lookup).
* Raises ``P0001`` with a descriptive message if tenant_id disagrees.
* NULL company_id or NULL tenant_id on NEW is also rejected (belt-and-
  braces on top of existing NOT NULL constraints).

Reversibility
-------------
``downgrade()`` drops all eight triggers and the shared trigger function.

Revision ID: 0128_tenant_id_coherence_trigger
Revises: 0127_drop_journal_tenant_default
Create Date: 2026-05-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0131_tenant_id_coherence_trigger"
down_revision: str | None = "0130_wizard_state_with_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FN_NAME = "assert_child_tenant_matches_company"

# Tables that carry a denormalised tenant_id that must agree with their
# company row.  Order is arbitrary — each gets its own trigger.
_TABLES = [
    "accounts",
    "contacts",
    "items",
    "tax_codes",
    "projects",
    "journal_templates",
    "departments",
    "cost_centres",
]


def upgrade() -> None:
    # 1. Shared trigger function (single statement, no semicolon issues).
    op.execute(
        sa.text(
            f"""CREATE OR REPLACE FUNCTION {_FN_NAME}()
            RETURNS trigger AS $$
            DECLARE
                v_company_tenant_id uuid;
            BEGIN
                IF NEW.company_id IS NULL THEN
                    RAISE EXCEPTION
                        'tenant_coherence: %.company_id must not be NULL',
                        TG_TABLE_NAME;
                END IF;

                SELECT tenant_id INTO v_company_tenant_id
                FROM companies
                WHERE id = NEW.company_id;

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
            $$ LANGUAGE plpgsql"""
        )
    )

    # 2. Per-table trigger — each DDL as a separate execute() call so
    #    asyncpg does not see multiple statements in one prepared statement.
    for table in _TABLES:
        trigger_name = f"trg_{table}_tenant_coherence"
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}")
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {trigger_name} "
                f"BEFORE INSERT OR UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {_FN_NAME}()"
            )
        )


def downgrade() -> None:
    for table in _TABLES:
        trigger_name = f"trg_{table}_tenant_coherence"
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}")
        )
    op.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {_FN_NAME}()")
    )
