"""Move the capture (bank-feed) tables into a dedicated ``capture`` schema.

Step 4 of the module extraction (gitea saebooks/saebooks #32; runbook
§§2.2/2.6/2.9 step 4). This mirrors the pre-accounting step-3 move
(``0172_preaccounting_schema``) exactly — same ``SET SCHEMA`` mechanism,
same ``search_path`` net, same RLS-survives-the-move reasoning — with two
capture-specific differences called out below.

What moves
----------
Exactly the four module-owned capture-state tables (runbook §2.2 —
verified against the ORM models):

* ``bank_feed_clients``        (0016/0029) — CompanyScoped; RLS Class B
* ``bank_feed_accounts``       (0016/0029) — CompanyScoped; RLS Class B
* ``bank_feed_issues``         (0016)      — global aggregator feed-health
  cache; NOT tenant/company scoped, so it carries NO RLS policy (nothing
  to re-verify beyond the schema move itself)
* ``bank_feed_external_creds`` (0086)      — tenant-scoped; RLS Class A

``wizard_state`` deliberately STAYS in ``public`` (runbook §2.2): it is
shared session scratch, also written by the ATO SBR worker
(``kind="ato_sbr"``), so moving it would create a second cross-module
reverse dependency. It is engine-owned and capture consumes it via API.

The engine reconciliation fact ``bank_statement_lines`` also STAYS in
``public`` — it is a ledger fact, not capture output (runbook §2.2). Note
the reverse FK ``bank_statement_lines.bank_feed_account_id ->
bank_feed_accounts.id`` (SET NULL, ``saebooks/models/bank_statement.py``):
a fact row points at a capture-owned row. That engine→module FK is the
exact reason this is a shared-DB schema split and not a separate database
— Postgres keeps a cross-schema FK valid but not a cross-database one. The
FK is re-pointed automatically by ``SET SCHEMA`` and is asserted intact by
``tests/test_rls_capture_schema.py``.

FKs / RLS survive SET SCHEMA — verification notes
-------------------------------------------------
* ``ALTER TABLE ... SET SCHEMA`` moves the table with its indexes,
  constraints, triggers and row-level-security policies attached. Inbound
  and outbound FKs are re-pointed by Postgres automatically (they
  reference table OIDs, not schema-qualified names), so
  ``bank_statement_lines.bank_feed_account_id`` (inbound) and every
  module→engine FK (``company_id``/``bank_feed_client_id`` ->
  ``companies``/``bank_feed_clients``, ``ledger_account_id`` ->
  ``accounts`` RESTRICT) stay intact.
* ``bank_feed_clients`` / ``bank_feed_accounts`` carry a Class-B
  ``tenant_isolation`` policy (``company_id IN (SELECT id FROM companies
  WHERE tenant_id = current_setting('app.current_tenant', true)::uuid)``,
  mig 0085) with FORCE RLS. ``bank_feed_external_creds`` carries the
  Class-A policy (direct ``tenant_id``, mig 0086). All three predicates
  reference columns + a GUC (+ the ``companies`` relation by OID for
  Class B), never a schema-qualified name, so the policies stay valid;
  only ``pg_policies.schemaname`` flips ``public``->``capture``. Re-proven
  live by the cross-tenant probes in ``tests/test_rls_capture_schema.py``.
* The ``*_status`` values are stored as TEXT (not native ENUM), so there
  is no type to move.
* PKs are ``gen_random_uuid()`` UUIDs — no owned sequences to worry about.

Capture-specific difference #1 — the SECURITY DEFINER enumerator
----------------------------------------------------------------
Migration 0084 installs ``bank_feeds_active_accounts_for_sync()`` — a
SECURITY DEFINER SQL function the sync-feeds CLI calls to enumerate every
tenant's active feed accounts under RLS. It references
``bank_feed_accounts`` by its **unqualified** name and pins
``SET search_path = pg_catalog, public`` on the function itself. A
function's own ``SET search_path`` OVERRIDES the session/database
search_path at call time, so once ``bank_feed_accounts`` lives in
``capture`` the function would fail with "relation bank_feed_accounts
does not exist" — a failure the DB-level search_path net below does NOT
cover, precisely because the function pins its own.

Fix: ``CREATE OR REPLACE`` the function with
``SET search_path = pg_catalog, public, capture`` (identical body). Per
Postgres, ``CREATE OR REPLACE FUNCTION`` preserves the function's owner
(``saebooks``, BYPASSRLS) and existing privileges (``GRANT EXECUTE`` to
``saebooks_app``), so no re-hardening is needed — we only widen the
pinned path. We move the tables FIRST, then replace the function, so the
LANGUAGE-sql body check (which runs under the function's own SET path)
resolves ``bank_feed_accounts`` in ``capture``. The downgrade restores the
original ``pg_catalog, public`` path after moving the tables back.

Capture-specific difference #2 — the search_path chain
------------------------------------------------------
0172 set ``search_path = public, preaccounting``. This migration EXTENDS
that to ``public, preaccounting, capture`` (never regresses 0172's value)
using the same three statements 0172 used (ALTER DATABASE + both roles).
The downgrade restores the value to 0172's ``public, preaccounting`` — a
plain ``RESET`` would wrongly drop ``preaccounting`` and break the
still-relocated pre-accounting tables, so we SET the reduced value
explicitly rather than RESET.

The read-only role ``saebooks_sql_ro`` (0087) needs no schema grant: it
holds ``pg_read_all_data``, which implicitly grants USAGE on every schema
and SELECT on every table — same as it relied on for ``preaccounting``.

search_path (so the schema-agnostic ORM keeps resolving)
--------------------------------------------------------
The ORM models are intentionally schema-agnostic (no ``__table_args__
schema=``) because the Cashbook SQLite backend has no schemas. Unqualified
names must still resolve on Postgres, so we set ``search_path`` on the
database (safety net for every connecting role) and, redundantly +
explicitly, on both roles the app connects as (owner + ``saebooks_app``).
``saebooks/db.py`` sets no hardcoded ``search_path`` in ``connect_args``,
so nothing overrides these; the deploy restarts the app (fresh
connections) and the test suite builds fresh engines, so no stale pooled
connection keeps the old path. ``ALTER DATABASE ... SET`` is transactional
DDL and runs safely inside the alembic per-migration transaction.

Downgrade
---------
Move each table back to ``public``, restore the SECDEF function's original
search_path, restore ``search_path`` to ``public, preaccounting`` on the
DB + both roles, drop the default-privilege entries, then DROP the (now
empty) schema. Fully reversible; no FK is dropped at any point.

Revision ID: 0173_capture_schema
Revises: 0172_preaccounting_schema
Create Date: 2026-07-04
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0173_capture_schema"
down_revision: str | None = "0172_preaccounting_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "capture"
_APP_ROLE = "saebooks_app"

# Order is cosmetic — SET SCHEMA re-points FKs regardless of table order —
# but we list parent (clients) before child (accounts) for readability.
_TABLES: tuple[str, ...] = (
    "bank_feed_clients",
    "bank_feed_accounts",
    "bank_feed_issues",
    "bank_feed_external_creds",
)

# Upgrade extends 0172's path; downgrade restores 0172's exact value.
_SEARCH_PATH_UP = "public, preaccounting, capture"
_SEARCH_PATH_DOWN = "public, preaccounting"

# 0084's SECURITY DEFINER enumerator, re-created with a search_path that
# also includes ``capture`` so it resolves the moved ``bank_feed_accounts``.
# Body is byte-identical to 0084; only the pinned path differs.
_FN_TEMPLATE = """
CREATE OR REPLACE FUNCTION bank_feeds_active_accounts_for_sync()
RETURNS TABLE (
    company_id UUID,
    tenant_id  UUID,
    account_id UUID
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = {search_path}
AS $$
    SELECT bfa.company_id,
           c.tenant_id,
           bfa.id AS account_id
    FROM bank_feed_accounts bfa
    JOIN companies c ON c.id = bfa.company_id
    WHERE bfa.revoked_at IS NULL;
$$;
"""

_FN_SEARCH_PATH_UP = "pg_catalog, public, capture"
_FN_SEARCH_PATH_DOWN = "pg_catalog, public"


def _quote_ident(name: str) -> str:
    """Double-quote an identifier, escaping embedded quotes.

    db / role names come from the live catalog (``current_database()`` /
    ``current_user``), not user input, but quoting keeps the generated DDL
    correct for names needing it and avoids any injection surprise.
    """
    return '"' + name.replace('"', '""') + '"'


def _app_role_exists(bind: sa.engine.Connection) -> bool:
    return (
        bind.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
            {"r": _APP_ROLE},
        ).first()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()

    # SQLite (Cashbook) never runs migrations — env.py short-circuits to
    # bootstrap_schema. Guard anyway so a stray invocation is a no-op
    # rather than an error (SQLite has no schemas / roles).
    if bind.dialect.name != "postgresql":
        return

    db_name = bind.execute(sa.text("SELECT current_database()")).scalar_one()
    current_role = bind.execute(sa.text("SELECT current_user")).scalar_one()

    # 1. Create the schema (owned by the migration/owner role).
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))

    # 2. Grant the app role access to the new schema + future objects in
    #    it. Table-level DML grants ride along with SET SCHEMA, but the
    #    role still needs USAGE on the schema itself, and the ALTER DEFAULT
    #    PRIVILEGES from 0056 only covered ``public``.
    if _app_role_exists(bind):
        op.execute(sa.text(f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {_APP_ROLE}"))
        op.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}"
            )
        )
        op.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
                f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {_APP_ROLE}"
            )
        )
        op.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
                f"GRANT EXECUTE ON FUNCTIONS TO {_APP_ROLE}"
            )
        )

    # 3. Move the tables. FKs (both directions — including the inbound
    #    bank_statement_lines.bank_feed_account_id fact FK), indexes,
    #    constraints and the RLS policies move with each table.
    for table in _TABLES:
        op.execute(sa.text(f"ALTER TABLE public.{table} SET SCHEMA {_SCHEMA}"))

    # 4. Re-assert DML grants on the moved tables (idempotent; grants
    #    survive SET SCHEMA, but this is cheap insurance).
    if _app_role_exists(bind):
        for table in _TABLES:
            op.execute(
                sa.text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON {_SCHEMA}.{table} TO {_APP_ROLE}"
                )
            )

    # 5. Re-point the 0084 SECURITY DEFINER enumerator at the moved table.
    #    The function pins its OWN search_path, which overrides the DB/role
    #    settings below at call time, so it must be widened to include
    #    ``capture`` or sync-feeds breaks. Tables are already moved (step 3)
    #    so the LANGUAGE-sql body check resolves. CREATE OR REPLACE keeps
    #    the owner (saebooks) + GRANT EXECUTE (saebooks_app) intact.
    op.execute(sa.text(_FN_TEMPLATE.format(search_path=_FN_SEARCH_PATH_UP)))

    # 6. search_path so the schema-agnostic ORM keeps resolving unqualified
    #    names. EXTENDS 0172's ``public, preaccounting`` to include
    #    ``capture`` — never regresses it. Database-level = safety net for
    #    every role; role-level = explicit for the two roles the app uses.
    db_id = _quote_ident(db_name)
    op.execute(
        sa.text(f"ALTER DATABASE {db_id} SET search_path TO {_SEARCH_PATH_UP}")
    )
    op.execute(
        sa.text(
            f"ALTER ROLE {_quote_ident(current_role)} IN DATABASE {db_id} "
            f"SET search_path TO {_SEARCH_PATH_UP}"
        )
    )
    if _app_role_exists(bind):
        op.execute(
            sa.text(
                f"ALTER ROLE {_APP_ROLE} IN DATABASE {db_id} "
                f"SET search_path TO {_SEARCH_PATH_UP}"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    db_name = bind.execute(sa.text("SELECT current_database()")).scalar_one()
    current_role = bind.execute(sa.text("SELECT current_user")).scalar_one()
    db_id = _quote_ident(db_name)

    # 1. Move the tables back to public (fully qualified source schema so
    #    resolution is unambiguous regardless of the active search_path).
    for table in _TABLES:
        op.execute(sa.text(f"ALTER TABLE {_SCHEMA}.{table} SET SCHEMA public"))

    # 2. Restore the SECDEF enumerator's original pinned search_path now
    #    that ``bank_feed_accounts`` is back in ``public`` (body check
    #    resolves). Mirrors 0084's original definition exactly.
    op.execute(sa.text(_FN_TEMPLATE.format(search_path=_FN_SEARCH_PATH_DOWN)))

    # 3. Restore search_path to 0172's value on the DB and both roles.
    #    SET (not RESET): a RESET would drop ``preaccounting`` too and
    #    break the still-relocated pre-accounting tables.
    op.execute(
        sa.text(f"ALTER DATABASE {db_id} SET search_path TO {_SEARCH_PATH_DOWN}")
    )
    op.execute(
        sa.text(
            f"ALTER ROLE {_quote_ident(current_role)} IN DATABASE {db_id} "
            f"SET search_path TO {_SEARCH_PATH_DOWN}"
        )
    )
    if _app_role_exists(bind):
        op.execute(
            sa.text(
                f"ALTER ROLE {_APP_ROLE} IN DATABASE {db_id} "
                f"SET search_path TO {_SEARCH_PATH_DOWN}"
            )
        )
        # Drop default-privilege entries we added for the schema so the
        # DROP SCHEMA below leaves no dangling grant records.
        op.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
                f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_APP_ROLE}"
            )
        )
        op.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
                f"REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {_APP_ROLE}"
            )
        )
        op.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
                f"REVOKE EXECUTE ON FUNCTIONS FROM {_APP_ROLE}"
            )
        )

    # 4. Drop the now-empty schema (RESTRICT is the default — errors loudly
    #    if anything unexpected still lives there).
    op.execute(sa.text(f"DROP SCHEMA IF EXISTS {_SCHEMA}"))
