"""Finalise saebooks_app role: password from env + grant refresh.

Lane 4 P0-1 follow-up. See:
- ``audit-trail/2026-05-23-overnight/04-rls-multi-tenant.md`` (P0-1)
- ``docs/db-role-split.md`` (deployment plan for the 5 stacks)

Background
----------
Migration ``0056_split_db_role`` created the ``saebooks_app`` role
(NOSUPERUSER + NOBYPASSRLS) and granted it DML on every table that
existed at the time. ``ALTER DEFAULT PRIVILEGES`` was issued so future
tables created by the migration-owner role inherit the same grants
automatically — and that has worked: ``has_table_privilege`` on
post-0056 tables (``change_log``, ``leave_balances``,
``paperless_webhook_secrets``, etc.) returns true.

Two things stopped 0056 from closing P0-1 on its own:

1. **No password.** The role was created without a password, so the
   API container could not log in as it. The intent (per the 0056
   docstring) was for the operator to ``ALTER ROLE ... PASSWORD ...``
   manually after the migration ran. That step was never wired into
   any compose / deploy / .env workflow and has not run in production.
2. **No deployment switch.** The runtime ``DATABASE_URL`` in the 5
   live stacks still points at the ``saebooks`` superuser role, so
   even after a password is set, RLS remains inert until each compose
   file flips to ``SAEBOOKS_APP_DATABASE_URL=…saebooks_app…``.

This migration closes step 1 (the DB-side piece). Step 2 is operator
work documented in ``docs/db-role-split.md``.

What it does
------------
1. Read ``SAEBOOKS_APP_DB_PASSWORD`` from the migration process'
   environment. If unset OR empty, the migration **refuses to run**
   with a clear error. The password is never hardcoded.
2. ``ALTER ROLE saebooks_app WITH PASSWORD '<env>'`` — idempotent;
   safe to re-run with the same or different password.
3. Belt-and-braces: re-assert NOSUPERUSER + NOBYPASSRLS + LOGIN.
   (0056 set these at CREATE; this is in case an operator hand-rolled
   the role with different attributes during the gap.)
4. Re-issue every GRANT from 0056. Idempotent — GRANT is a no-op when
   the privilege is already present. This catches the case where the
   ``ALTER DEFAULT PRIVILEGES`` from 0056 was issued by a role that
   was not the actual creator of subsequent tables (e.g. when the
   test stack ran migrations as ``saebooks_test``).

Password security
-----------------
PostgreSQL stores the password as a SCRAM-SHA-256 hash by default
(set by ``password_encryption = 'scram-sha-256'`` in postgresql.conf,
which is the default since PG14). The cleartext appears once in this
migration's SQL stream but is never persisted to disk in cleartext.
The alembic logs do not log SQL bodies by default.

``ALTER ROLE ... PASSWORD`` does NOT accept bind parameters at the
protocol level — it is a utility statement, not a DML statement, and
asyncpg surfaces this as ``the server expects 0 arguments for this
query``. The DO-block / EXECUTE-format trick has the same limitation:
the DO body is parsed *as text*, the outer protocol still sees zero
parameter slots.

We therefore validate the password against a strict character class
(SCRAM-safe ASCII: alphanumeric + a small set of unambiguous
punctuation) and embed it as a single-quoted SQL literal with the
standard ``''`` escape for any internal single quote. The character
class is narrower than what Postgres accepts for a password — the
restriction is on what we will *write* in this migration, not on
what Postgres allows. Tighter is safer.

Revision ID: 0128_app_role
Revises: 0127_drop_journal_tenant_default
Create Date: 2026-05-24
"""
from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0128_app_role"
down_revision: str | None = "0127_drop_journal_tenant_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "saebooks_app"


import re

# SCRAM-safe ASCII set we permit in the migration-time password literal.
# This is a *whitelist on what we will embed*, not on what Postgres
# accepts. Restricting to characters that have no SQL-syntactic
# meaning lets us interpolate the value into the ALTER ROLE statement
# without a quoting / escaping bug ever being possible. Operators
# generating credentials with ``openssl rand -base64 48`` produce
# strings inside this set (with `+/=`).
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9+/=._\-]{16,256}$")


def _get_password_from_env() -> str:
    """Resolve the saebooks_app password from the migration environment.

    Looks up ``SAEBOOKS_APP_DB_PASSWORD``. Empty / unset / unsafe
    characters / too short → refuse.

    The hard-fail is deliberate: silently leaving the password unset
    is what blocked the 0056 fix from landing for a month. We make
    the absence loud.
    """
    pw = os.environ.get("SAEBOOKS_APP_DB_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError(
            "SAEBOOKS_APP_DB_PASSWORD must be set in the migration "
            "environment so 0128_app_role can set the saebooks_app "
            "password. See docs/db-role-split.md for how this slots "
            "into each compose stack."
        )
    if not _PASSWORD_RE.fullmatch(pw):
        raise RuntimeError(
            "SAEBOOKS_APP_DB_PASSWORD must be 16-256 chars from the "
            "SCRAM-safe set [A-Za-z0-9+/=._-]. Use "
            "`openssl rand -base64 48 | tr -d '\\n'` which produces "
            "output inside this set."
        )
    return pw


def upgrade() -> None:
    bind = op.get_bind()

    # If the role does not exist (e.g. a fresh-from-empty DB that
    # somehow bypassed 0056 — shouldn't happen, but the test stack
    # init.sql also creates it, so a re-create is harmless and
    # idempotent), create it with the right attributes. The
    # ``CREATE ROLE`` in 0056 used a ``DO $$ ... $$`` block keyed on
    # ``pg_roles`` — same pattern here.
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}'
                ) THEN
                    CREATE ROLE {_APP_ROLE}
                        LOGIN
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

    # Belt-and-braces — re-assert the attributes 0056 set, in case
    # somebody hand-altered them between 0056 and 0128. This is a
    # no-op on any role that already has these flags.
    bind.execute(
        sa.text(
            f"ALTER ROLE {_APP_ROLE} "
            "LOGIN NOSUPERUSER NOBYPASSRLS INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION"
        )
    )

    # Make sure the migration-running role can manage saebooks_app
    # (mirrors the 0056 ``GRANT {_APP_ROLE} TO {current_role}``).
    current_role = bind.execute(sa.text("SELECT current_user")).scalar_one()
    bind.execute(sa.text(f"GRANT {_APP_ROLE} TO {current_role}"))

    # Set the password from env. ``ALTER ROLE ... PASSWORD`` is a
    # utility statement and does not accept bind parameters at the
    # protocol level (asyncpg surfaces this as "the server expects 0
    # arguments for this query"). The DO/EXECUTE-format trick has the
    # same limitation. So we embed the literal directly — which is
    # safe because ``_get_password_from_env`` already restricted the
    # value to a SCRAM-safe character class that contains no SQL
    # syntax.
    pw = _get_password_from_env()
    bind.execute(
        sa.text(f"ALTER ROLE {_APP_ROLE} WITH PASSWORD '{pw}'")
    )

    # Re-issue all of the 0056 GRANTs. Each is idempotent.
    bind.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}"))
    bind.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON ALL TABLES IN SCHEMA public TO {_APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"GRANT USAGE, SELECT, UPDATE "
            f"ON ALL SEQUENCES IN SCHEMA public TO {_APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {_APP_ROLE}"
        )
    )

    # Future-table grants: ALTER DEFAULT PRIVILEGES is per-role. The
    # 0056 invocation ran for whichever role executed the migration
    # at the time. We re-issue it under whichever role is running
    # this migration. If 0056 already covered this role, the entry
    # is replaced (semantically equivalent); if not, the new entry
    # picks up future objects this role creates.
    bind.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {_APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT EXECUTE ON FUNCTIONS TO {_APP_ROLE}"
        )
    )

    # Verification: assert the role attributes are what we expect.
    # If somebody manually granted SUPERUSER or BYPASSRLS between
    # 0056 and now (re-introducing the P0-1 bypass), fail loudly so
    # the operator notices.
    row = bind.execute(
        sa.text(
            "SELECT rolsuper, rolbypassrls, rolcanlogin "
            f"FROM pg_roles WHERE rolname = '{_APP_ROLE}'"
        )
    ).first()
    if row is None:
        raise RuntimeError(
            f"Role {_APP_ROLE} missing after upgrade — DDL did not apply"
        )
    if row[0] or row[1]:
        raise RuntimeError(
            f"Role {_APP_ROLE} has rolsuper={row[0]} rolbypassrls={row[1]} — "
            "P0-1 still in force. Manually run "
            f"ALTER ROLE {_APP_ROLE} NOSUPERUSER NOBYPASSRLS and re-run."
        )
    if not row[2]:
        raise RuntimeError(
            f"Role {_APP_ROLE} has rolcanlogin=false — cannot be used "
            "by the API container."
        )


def downgrade() -> None:
    """Roll back to the pre-0128 state.

    We deliberately do NOT drop the role here — that's 0056's job,
    and dropping it as part of 0128 downgrade would leave the running
    API stranded if 0128 was reverted in isolation. Instead we just
    null the password back out, matching the 0056-created state.
    """
    bind = op.get_bind()
    # Null the password (LOGIN becomes effectively disabled).
    bind.execute(sa.text(f"ALTER ROLE {_APP_ROLE} WITH PASSWORD NULL"))
