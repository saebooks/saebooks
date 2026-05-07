"""P0 cross-tenant leak — FORCE row-level security + complete missing policies.

Background
----------
Three compounding bugs leaked rows across tenants (see
``audit-trail/02-cross-tenant-leak-diagnosis.md``):

1. ``_first_company_id()`` returned the oldest active company, ignoring
   the caller's tenant.
2. RLS was enabled on six tables but the API role (``saebooks2`` /
   ``saebooks``) is the table owner, and table owners bypass RLS unless
   ``FORCE ROW LEVEL SECURITY`` is set. The policy was a velvet rope.
3. Per-request ``SET LOCAL app.current_tenant`` was never wired — the
   helper opened its own session and the GUC never reached handler
   sessions.

This migration is the immediate stop-gap addressing #2 and #3-on-the-DB-side:

* For every table carrying a ``tenant_id`` column, enable RLS, force
  RLS so the owner is also bound by the policy, and create the
  ``tenant_isolation`` policy if it does not already exist.
* The policy is unchanged in shape from migration 0041:
  ``USING (tenant_id = current_setting('app.current_tenant', true)::uuid)``
  with the same predicate for ``WITH CHECK`` so writes are also gated.

Scope
-----
Tables with a ``tenant_id`` column (verified against
``information_schema.columns``):

    accounts, bank_statement_lines, bills, budgets, companies,
    contacts, credit_notes, fixed_assets, invoices, items,
    journal_entries, payments, projects, recurring_invoices,
    tax_codes, users.

Line / child tables (``invoice_lines``, ``bill_lines``,
``payment_allocations``, ``journal_lines``, ``credit_note_lines``,
``recurring_invoice_lines``) have no ``tenant_id`` column and therefore
no direct policy here. They are scoped via their parent at the service
layer; cross-tenant escape would require a service-layer bug joining
without the parent's tenant filter, and is addressed by the
session-scoped ``SET LOCAL app.current_tenant`` work in the follow-up
refactor.

Auxiliary tables (``audit_snapshots``, ``change_log``, ``idempotency_keys``,
``tenants``) are not tenant-scoped here either — they're either global
(``tenants``) or row-id-keyed and only reachable via a tenant-scoped
parent lookup.

Verifying the migration
-----------------------
After ``alembic upgrade head``::

    SELECT relname, relrowsecurity, relforcerowsecurity
    FROM pg_class
    WHERE relname IN (
        'accounts','bank_statement_lines','bills','budgets',
        'companies','contacts','credit_notes','fixed_assets',
        'invoices','items','journal_entries','payments','projects',
        'recurring_invoices','tax_codes','users'
    )
    ORDER BY relname;

Every row should have ``relrowsecurity = t`` and
``relforcerowsecurity = t``.

    SELECT tablename, policyname FROM pg_policies ORDER BY tablename;

Should list ``tenant_isolation`` for all 16 tables above.

Empirical proof of the fix:

    SET app.current_tenant = '00000000-0000-0000-0000-000000000001';
    SELECT count(*) FROM contacts;   -- only Default tenant rows
    SET app.current_tenant = '<other-tenant>';
    SELECT count(*) FROM contacts;   -- only that tenant's rows
    RESET app.current_tenant;
    SELECT count(*) FROM contacts;   -- with FORCE RLS and the
                                     -- recommended_check predicate this
                                     -- returns 0, NOT every row.

Reversibility
-------------
``downgrade()`` removes the policies created by this migration (those
not present before, i.e. the 10 tables that did not have a policy in
migration 0041) and clears ``relforcerowsecurity`` on all 16. It does
NOT disable row security on the 6 tables that 0041 enabled (so the
state after downgrade matches what 0041 left behind).

Revision ID: 0055_force_rls_complete
Revises: 0054_stripe_payment_link
Create Date: 2026-04-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0055_force_rls_complete"
down_revision: str | None = "0054_stripe_payment_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that already had RLS enabled + tenant_isolation policy
# from migration 0041. They get FORCE here; we do NOT recreate the
# policy.
_TABLES_WITH_EXISTING_POLICY: tuple[str, ...] = (
    "accounts",
    "companies",
    "contacts",
    "items",
    "tax_codes",
    "users",
)

# Tables that have a ``tenant_id`` column added by migrations 0042-0051
# but never got RLS enabled or a policy. They get the full treatment.
_TABLES_NEEDING_POLICY: tuple[str, ...] = (
    "bank_statement_lines",
    "bills",
    "budgets",
    "credit_notes",
    "fixed_assets",
    "invoices",
    "journal_entries",
    "payments",
    "projects",
    "recurring_invoices",
)

_ALL_TENANT_TABLES: tuple[str, ...] = (
    *_TABLES_WITH_EXISTING_POLICY,
    *_TABLES_NEEDING_POLICY,
)

# Same predicate shape as migration 0041 but with WITH CHECK as well so
# inserts/updates writing a foreign tenant_id are blocked, not silently
# accepted.
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING


def upgrade() -> None:
    # 1. Add a tenant_isolation policy to the 10 tables that have a
    #    tenant_id column but no policy yet. Use FOR ALL with USING +
    #    WITH CHECK so the same predicate applies to reads and writes.
    for table in _TABLES_NEEDING_POLICY:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"  # noqa: S608
            )
        )
        # CREATE POLICY ... IF NOT EXISTS isn't supported until PG 17;
        # we're on PG 16, so guard with DROP IF EXISTS first to keep
        # the migration idempotent if a previous attempt got partway.
        op.execute(
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
            )
        )

    # 2. The 6 tables from 0041 already have a policy with USING only.
    #    Recreate them with FOR ALL + WITH CHECK so writes are gated
    #    too — RLS without WITH CHECK lets you INSERT a row with a
    #    foreign tenant_id and only blocks reads of it on the next
    #    SELECT.
    for table in _TABLES_WITH_EXISTING_POLICY:
        op.execute(
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
            )
        )

    # 3. FORCE RLS on every tenant-scoped table so the table owner
    #    (the role the API connects as) is bound by the policy too.
    #    Without this, owner-bypass renders RLS a velvet rope.
    for table in _ALL_TENANT_TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"  # noqa: S608
            )
        )


def downgrade() -> None:
    # Reverse step 3: clear FORCE on every tenant-scoped table.
    for table in _ALL_TENANT_TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"  # noqa: S608
            )
        )

    # Reverse step 2: restore the USING-only policy that 0041 created
    # on the 6 original tables.
    for table in _TABLES_WITH_EXISTING_POLICY:
        op.execute(
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING {_USING}"
            )
        )

    # Reverse step 1: remove the 10 newly-added policies and disable
    # row security on those tables.
    for table in _TABLES_NEEDING_POLICY:
        op.execute(
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"  # noqa: S608
            )
        )
