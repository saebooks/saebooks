"""P0 cross-tenant leak — split DB role into owner + app.

Step 2 of cross-tenant leak fix.

Background
----------
Migration 0055 forces RLS on every tenant-scoped table, but Postgres
superusers bypass RLS regardless of FORCE. The historical ``saebooks``
role connects with ``rolsuper=t`` and ``rolbypassrls=t``, so RLS does
nothing for the running API.

Resolution
----------
Create a second role, ``saebooks_app``, that:

* Has LOGIN.
* Is NOT a superuser, does NOT inherit BYPASSRLS.
* Holds DML privileges (SELECT/INSERT/UPDATE/DELETE) on every existing
  user table, USAGE/SELECT on every sequence, and EXECUTE on every
  function — but is NOT the owner of any object.

The runtime API container connects as ``saebooks_app`` and is therefore
bound by the ``tenant_isolation`` policy. Migrations and admin tooling
keep using the original ``saebooks`` role (the owner) so DDL still
works without RLS friction.

Password
--------
Postgres ``CREATE ROLE`` cannot take a password from a SQL parameter
without exposing it in the migration file or relying on superuser
``SCRAM-SHA-256`` hash injection. The migration creates the role
without a password (so ``LOGIN`` will fail until one is set) and
documents the manual step:

    -- After ``alembic upgrade head``, set the password:
    psql -U saebooks -d saebooks -c "
        ALTER ROLE saebooks_app WITH PASSWORD 'YOUR_SECURE_PASSWORD';
    "

    -- Then update the API container's environment to use the new role:
    SAEBOOKS_DATABASE_URL=postgresql+asyncpg://saebooks_app:YOUR_SECURE_PASSWORD@db:5432/saebooks

The API container also needs ``SAEBOOKS_APP_DB_PASSWORD`` set if you
prefer the new explicit env var (added by the same change set in
``saebooks/config.py``).

Idempotency
-----------
``CREATE ROLE`` errors if the role already exists. We wrap in a
``DO $$...$$`` block that checks ``pg_roles`` first so re-running the
migration after a partial failure is safe.

The privilege grants are also re-issued unconditionally — Postgres
GRANT is idempotent.

Reversibility
-------------
``downgrade()`` revokes all privileges, removes the default-privilege
entries that future-grant new objects, and drops the role. The role
must have no owned objects for DROP to succeed; since this migration
deliberately never makes ``saebooks_app`` an owner, that's a no-op
in practice.

Revision ID: 0056_split_db_role
Revises: 0055_force_rls_complete
Create Date: 2026-04-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0056_split_db_role"
down_revision: str | None = "0055_force_rls_complete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "saebooks_app"


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create the role idempotently.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}'
                ) THEN
                    -- LOGIN so the API can connect as this role; NOSUPERUSER
                    -- and explicit NOBYPASSRLS so FORCE row security binds it.
                    -- INHERIT so any future role-grants flow through.
                    -- No password set here — see migration docstring.
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

    # The role's owner — i.e. who can ALTER it. Whoever runs the
    # migration is the role's owner, but we explicitly grant the owner
    # role to itself anyway so ``saebooks`` (the migration role) can
    # still touch ``saebooks_app`` if needed.
    current_role = bind.execute(sa.text("SELECT current_user")).scalar_one()
    op.execute(
        sa.text(f"GRANT {_APP_ROLE} TO {current_role}")
    )

    # 2. USAGE on every existing schema (we only have public, but be
    # explicit so future schemas don't surprise us).
    op.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}"))

    # 3. DML on every existing table in public.
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON ALL TABLES IN SCHEMA public TO {_APP_ROLE}"
        )
    )

    # 4. Sequences — needed for nextval() on serial / identity columns.
    op.execute(
        sa.text(
            f"GRANT USAGE, SELECT, UPDATE "
            f"ON ALL SEQUENCES IN SCHEMA public TO {_APP_ROLE}"
        )
    )

    # 5. Functions — required for any pgcrypto / custom function calls.
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {_APP_ROLE}"
        )
    )

    # 6. Default privileges — apply the same grants to objects created
    #    by future migrations (run as ``saebooks``, the owner role).
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {_APP_ROLE}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT EXECUTE ON FUNCTIONS TO {_APP_ROLE}"
        )
    )


def downgrade() -> None:
    # Drop default-privilege entries first so the role has no future
    # grants pointing at it.
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_APP_ROLE}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {_APP_ROLE}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"REVOKE EXECUTE ON FUNCTIONS FROM {_APP_ROLE}"
        )
    )

    # Revoke standing privileges.
    op.execute(
        sa.text(
            f"REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM {_APP_ROLE}"
        )
    )
    op.execute(
        sa.text(
            f"REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES "
            f"IN SCHEMA public FROM {_APP_ROLE}"
        )
    )
    op.execute(
        sa.text(
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f"IN SCHEMA public FROM {_APP_ROLE}"
        )
    )
    op.execute(sa.text(f"REVOKE USAGE ON SCHEMA public FROM {_APP_ROLE}"))

    # Drop the role.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}'
                ) THEN
                    DROP ROLE {_APP_ROLE};
                END IF;
            END
            $$;
            """
        )
    )
