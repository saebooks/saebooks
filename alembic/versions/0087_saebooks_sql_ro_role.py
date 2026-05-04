"""Create the ``saebooks_sql_ro`` role for the admin SQL browser tool.

Cat-C admin (Worker W5) — Phase: ad-hoc SQL with strict read-only role.

Background
----------
The legacy SQL browser (``saebooks/routers/admin.py`` + ``services/sql_tool.py``)
relied on `SET TRANSACTION READ ONLY` to keep ad-hoc queries from mutating
data. That stops INSERT/UPDATE/DELETE/DDL but does NOT stop a Postgres
superuser from reading server-side files via ``pg_read_server_files``,
``pg_read_binary_file``, ``COPY ... FROM PROGRAM``, ``lo_export``, etc. As
long as the API container connects with a role that can call those
functions (the legacy ``saebooks`` owner is a superuser; even
``saebooks_app`` inherits public EXECUTE on most of them), an admin with
SQL-tool access has effective shell-on-server.

Resolution
----------
Create ``saebooks_sql_ro``:

* LOGIN role (so the SQL service can open a discrete connection as this
  role for every read query).
* NOSUPERUSER, NOBYPASSRLS — RLS still applies even on read-only paths
  so a misconfigured tenant binding cannot leak across tenants.
* Granted ``pg_read_all_data`` so it can SELECT from every existing
  *and future* table without a per-table grant — this is the postgres
  built-in role for read-only dump/replication scenarios.
* REVOKE EXECUTE on the dangerous file/program/lo functions from BOTH
  ``saebooks_sql_ro`` AND ``public`` — REVOKE from the role alone is
  insufficient because the default grant on these functions is to
  ``public``, and ``saebooks_sql_ro`` inherits the ``public`` grant.

Password
--------
Set from the ``SAEBOOKS_SQL_RO_PASSWORD`` env var at upgrade time.
Mirrors the manual-password story for ``saebooks_app`` (migration 0056)
but threaded through env so an automated ``alembic upgrade head`` works
end-to-end. If the env var is empty, upgrade aborts — we refuse to
create a LOGIN role without a password.

Idempotency
-----------
``CREATE ROLE`` is wrapped in ``DO $$ ... IF NOT EXISTS`` so re-running
after a partial failure is safe. ``GRANT`` and ``REVOKE`` are idempotent
in PostgreSQL. ``ALTER ROLE ... PASSWORD`` is rerun on every upgrade so
rotating the env var + ``alembic upgrade head`` rotates the password.

Reversibility
-------------
``downgrade()`` revokes login + drops the role. The role must own no
objects for DROP to succeed; this migration deliberately never makes
``saebooks_sql_ro`` an owner so downgrade is clean.

Revision ID: 0087_saebooks_sql_ro_role
Revises: 0086_pay_runs (dependency on whichever 0086 lands first); falls
back to 0085 if no 0086 has been merged when this rebases.
Create Date: 2026-05-04
"""
from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0087_saebooks_sql_ro_role"
# Sit on top of 0085 (the last migration in main at branch creation).
# If a sibling worker lands a 0086 that this migration logically follows,
# rebase the down_revision to that on merge.
down_revision: str | None = "0086_bank_feed_external_creds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RO_ROLE = "saebooks_sql_ro"

# Functions that can read host-side files, write host-side files, or
# escape into the operating system. ``COPY ... FROM PROGRAM`` is
# blocked at the engine level for non-superusers and so is not in this
# list; the SQL tool's security smoke test (test_sql_tool_role.py)
# verifies the engine-level block stays in place.
_DANGEROUS_FUNCTIONS: tuple[str, ...] = (
    "pg_catalog.pg_read_server_files(text)",
    "pg_catalog.pg_write_server_files(text, bytea)",
    "pg_catalog.pg_read_binary_file(text)",
    "pg_catalog.pg_read_binary_file(text, bigint, bigint)",
    "pg_catalog.pg_read_binary_file(text, bigint, bigint, boolean)",
    "pg_catalog.lo_export(oid, text)",
    "pg_catalog.lo_import(text)",
    "pg_catalog.lo_import(text, oid)",
    # Listening / dblink would let this role spawn outbound connections
    # if the dblink extension is ever installed. Belt-and-braces revoke.
    "pg_catalog.pg_ls_dir(text)",
    "pg_catalog.pg_ls_dir(text, boolean, boolean)",
    "pg_catalog.pg_stat_file(text)",
    "pg_catalog.pg_stat_file(text, boolean)",
)


def upgrade() -> None:
    password = os.environ.get("SAEBOOKS_SQL_RO_PASSWORD", "").strip()
    if not password:
        raise RuntimeError(
            "Migration 0087 requires SAEBOOKS_SQL_RO_PASSWORD in the "
            "environment to set the saebooks_sql_ro role password. "
            "Set it in .env (or pass via the alembic command env) before "
            "running upgrade head."
        )

    # 1. Create the role idempotently with NOLOGIN — we add LOGIN and
    #    the password in step 4 so a partial failure between create and
    #    password-set never leaves an unauthenticated LOGIN role.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_RO_ROLE}'
                ) THEN
                    CREATE ROLE {_RO_ROLE}
                        NOLOGIN
                        NOSUPERUSER
                        NOBYPASSRLS
                        INHERIT
                        NOCREATEDB
                        NOCREATEROLE
                        NOREPLICATION;
                END IF;
            END
            $$;
            """
        )
    )

    # 2. pg_read_all_data is a built-in postgres role (PG14+) that grants
    #    SELECT + USAGE on every table/sequence/schema, including future
    #    objects. Cleaner than maintaining per-table grants for RO.
    op.execute(sa.text(f"GRANT pg_read_all_data TO {_RO_ROLE}"))
    op.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {_RO_ROLE}"))

    # 3. REVOKE the dangerous functions from BOTH the role and ``public``
    #    so the role can't inherit a public grant. ``IF EXISTS`` is not
    #    available on REVOKE in PG16, so we wrap in a DO block that
    #    inspects ``pg_proc`` first to keep this idempotent across PG
    #    minor versions where some signatures may not exist.
    for sig in _DANGEROUS_FUNCTIONS:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM pg_proc p
                        JOIN pg_namespace n ON n.oid = p.pronamespace
                        WHERE n.nspname || '.' || p.proname || '(' ||
                              pg_get_function_identity_arguments(p.oid) || ')'
                              = '{sig}'
                    ) THEN
                        EXECUTE 'REVOKE EXECUTE ON FUNCTION {sig} '
                                'FROM {_RO_ROLE}';
                        EXECUTE 'REVOKE EXECUTE ON FUNCTION {sig} '
                                'FROM PUBLIC';
                    END IF;
                END
                $$;
                """
            )
        )

    # 4. Set the password and enable LOGIN. Done in one ALTER so the
    #    role transitions from "exists, no login" to "exists, login with
    #    password" atomically. ``ALTER ROLE ... PASSWORD`` is idempotent
    #    so re-running this step on an existing role rotates the
    #    password — useful for password rotation via a fresh upgrade.
    # Quote the password literal safely. PostgreSQL doesn't allow bind
    # parameters in ALTER ROLE, so we have to interpolate. Reject any
    # password containing a single quote — the password generator on
    # the API side restricts the alphabet to base64-url, so this is a
    # safety net not a hot path.
    if "'" in password:
        raise RuntimeError(
            "SAEBOOKS_SQL_RO_PASSWORD must not contain single quotes; "
            "regenerate from a base64-url alphabet."
        )
    op.execute(
        sa.text(
            f"ALTER ROLE {_RO_ROLE} WITH LOGIN PASSWORD '{password}'"
        )
    )


def downgrade() -> None:
    # 1. Strip login so any open sessions can finish their statements
    #    but no new connections can come in as this role.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_RO_ROLE}'
                ) THEN
                    ALTER ROLE {_RO_ROLE} WITH NOLOGIN;
                END IF;
            END
            $$;
            """
        )
    )

    # 2. Revoke role grants so DROP doesn't fail on dangling membership.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_RO_ROLE}'
                ) THEN
                    REVOKE pg_read_all_data FROM {_RO_ROLE};
                    REVOKE USAGE ON SCHEMA public FROM {_RO_ROLE};
                END IF;
            END
            $$;
            """
        )
    )

    # 3. Drop the role.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_RO_ROLE}'
                ) THEN
                    DROP ROLE {_RO_ROLE};
                END IF;
            END
            $$;
            """
        )
    )
