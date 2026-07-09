"""Move pre-accounting tables into a dedicated ``preaccounting`` schema.

Step 3 of the pre-accounting module extraction (gitea saebooks/saebooks
#32; runbook §1.8 step 3). Steps 1 (engine endpoints) and 2 (two-phase
in-process conversions) already shipped on main.

What moves
----------
Exactly the five module-owned pre-accounting tables (all genuinely
pre-GL — none post a journal):

* ``quotes``               (0095)
* ``quote_lines``          (0095)  — scoped via parent, no direct policy
* ``purchase_orders``      (0094)
* ``purchase_order_lines`` (0094)  — scoped via parent, no direct policy
* ``time_entries``         (0109)

``time_entries`` has **no companion line table** (verified against
``saebooks/models/time_entry.py`` — a single ``__tablename__`` only), so
the runbook's "6 tables" was one too many; the correct set is five.

``document_counters`` deliberately stays in ``public`` — it is shared
numbering state consumed (not owned) by the module (runbook §1.4).

Why a schema move and not a separate database
---------------------------------------------
``invoices.source_quote_id -> quotes.id`` (SET NULL,
``saebooks/models/invoice.py:190``) is an engine→module reverse FK.
Postgres keeps FKs valid across schemas in one database but not across
databases, so the tables move to a schema in the SAME database and every
FK — in both directions — survives ``SET SCHEMA`` untouched.

FKs / RLS survive SET SCHEMA — verification notes
-------------------------------------------------
* ``ALTER TABLE ... SET SCHEMA`` moves the table with its indexes,
  constraints, triggers and **row-level-security policies** attached.
  Inbound and outbound FKs are automatically re-pointed by Postgres
  (they reference table OIDs, not schema-qualified names), so
  ``invoices.source_quote_id -> quotes.id`` and every module→engine FK
  (``tenant_id``/``company_id``/``customer_id``/... ) stay intact.
* The three parent tables each carry a ``tenant_isolation`` policy
  (``quotes``/``purchase_orders``/``time_entries``, created in 0094/0095/
  0109) with ``FORCE ROW LEVEL SECURITY``. **pg_policies naming checked**:
  ``pg_policies.tablename`` is the *relname* and ``policyname`` is
  ``tenant_isolation`` — both are schema-independent, and the policy
  expression is ``tenant_id = current_setting('app.current_tenant',
  true)::uuid`` which references a *column* + a GUC, never a
  schema-qualified relation. So the policy names and predicates stay
  valid after the move; only ``pg_policies.schemaname`` flips
  ``public``→``preaccounting``. Re-verified live by the cross-tenant
  probe in ``tests/test_rls_preaccounting_schema.py``.
* The ``*_status`` ENUM types stay in ``public``; moved-table columns
  reference them by OID, so no type move is needed.
* PKs are ``gen_random_uuid()`` UUIDs — there are no owned sequences to
  worry about.

search_path (so the schema-agnostic ORM keeps resolving)
--------------------------------------------------------
The ORM models are intentionally **schema-agnostic** — no
``__table_args__ schema=`` — because the Cashbook SQLite backend has no
schemas (``saebooks/db.py``/``bootstrap_schema``). Unqualified names
(``quotes``, ``time_entries``, ...) must therefore still resolve on
Postgres after the move. We do that with ``search_path`` set on the
**database** (and, redundantly + explicitly, on both connecting roles):

* ``ALTER DATABASE <db> SET search_path TO public, preaccounting`` — the
  safety net. This applies to *every* role that connects to the DB
  (owner, ``saebooks_app``, the pre-auth login role, and the test
  stack's ``saebooks_test`` owner) without having to enumerate them, so
  it is strictly more robust than role-only settings. ``public`` is kept
  first so engine tables continue to resolve exactly as before.
* ``ALTER ROLE <current_user> IN DATABASE <db> SET search_path ...`` and
  ``ALTER ROLE saebooks_app IN DATABASE <db> SET search_path ...`` —
  explicit belt-and-braces for the two roles the app actually uses
  (owner for login/CLI/migrations, ``saebooks_app`` for RLS-bound
  request traffic). Guarded on ``saebooks_app`` existence.

``saebooks/db.py`` sets **no** hardcoded ``search_path`` in
``connect_args`` (checked), so nothing overrides these settings. The
settings apply at connect time; the deploy restarts the app (fresh
connections) and the test suite builds fresh engines, so no stale pooled
connection keeps the old path. ``ALTER DATABASE ... SET`` is transactional
DDL and runs safely inside the alembic per-migration transaction.

Downgrade
---------
``SET SCHEMA public`` for each table, RESET the search_path on the DB and
both roles, then DROP the (now empty) schema. Fully reversible; no FK is
dropped at any point.

Revision ID: 0172_preaccounting_schema
Revises: 0171_invoice_letterhead_parity
Create Date: 2026-07-03
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0172_preaccounting_schema"
down_revision: str | None = "0171_invoice_letterhead_parity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "preaccounting"
_APP_ROLE = "saebooks_app"

# Order: parents before their line children is not required for SET SCHEMA
# (FKs are re-pointed regardless), but we keep a stable, readable order.
_TABLES: tuple[str, ...] = (
    "quotes",
    "quote_lines",
    "purchase_orders",
    "purchase_order_lines",
    "time_entries",
)

_SEARCH_PATH = "public, preaccounting"


def _quote_ident(name: str) -> str:
    """Double-quote an identifier, escaping embedded quotes.

    db / role names come from the live catalog (``current_database()`` /
    ``current_user``), not user input, but quoting keeps the generated
    DDL correct for names needing it and avoids any injection surprise.
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
    #    role still needs USAGE on the schema itself, and ALTER DEFAULT
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

    # 3. Move the tables. FKs (both directions), indexes, constraints and
    #    the tenant_isolation RLS policies move with each table.
    for table in _TABLES:
        op.execute(sa.text(f"ALTER TABLE public.{table} SET SCHEMA {_SCHEMA}"))

    # 4. Re-assert DML grants on the moved tables (idempotent; grants
    #    survive SET SCHEMA, but this is cheap insurance against any
    #    environment where they did not).
    if _app_role_exists(bind):
        for table in _TABLES:
            op.execute(
                sa.text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON {_SCHEMA}.{table} TO {_APP_ROLE}"
                )
            )

    # 5. search_path so the schema-agnostic ORM keeps resolving
    #    unqualified names. Database-level = safety net for every role;
    #    role-level = explicit for the two roles the app connects as.
    db_id = _quote_ident(db_name)
    op.execute(
        sa.text(f"ALTER DATABASE {db_id} SET search_path TO {_SEARCH_PATH}")
    )
    op.execute(
        sa.text(
            f"ALTER ROLE {_quote_ident(current_role)} IN DATABASE {db_id} "
            f"SET search_path TO {_SEARCH_PATH}"
        )
    )
    if _app_role_exists(bind):
        op.execute(
            sa.text(
                f"ALTER ROLE {_APP_ROLE} IN DATABASE {db_id} "
                f"SET search_path TO {_SEARCH_PATH}"
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

    # 2. RESET search_path on the DB and both roles.
    op.execute(sa.text(f"ALTER DATABASE {db_id} RESET search_path"))
    op.execute(
        sa.text(
            f"ALTER ROLE {_quote_ident(current_role)} IN DATABASE {db_id} "
            f"RESET search_path"
        )
    )
    if _app_role_exists(bind):
        op.execute(
            sa.text(
                f"ALTER ROLE {_APP_ROLE} IN DATABASE {db_id} RESET search_path"
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

    # 3. Drop the now-empty schema (RESTRICT is the default — errors loudly
    #    if anything unexpected still lives there).
    op.execute(sa.text(f"DROP SCHEMA IF EXISTS {_SCHEMA}"))
