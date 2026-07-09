-- Bootstrap roles for the ephemeral test database.
--
-- The production DB has a ``saebooks`` superuser (BYPASSRLS) and a
-- ``saebooks_app`` application role (NOBYPASSRLS).  Several Alembic
-- migrations reference these role names by literal string (e.g.
-- ``ALTER FUNCTION ... OWNER TO saebooks``).  We create stub versions
-- here so migrations run cleanly against a fresh test DB whose
-- superuser is named ``saebooks_test``.
--
-- This script runs once when the postgres container is initialised
-- (docker-entrypoint-initdb.d).  It is idempotent: DO blocks guard
-- against re-creation on a persistent volume.

DO $$
BEGIN
    -- ``saebooks`` — the owner/BYPASSRLS role that migrations refer to.
    -- In the test stack we don't actually need BYPASSRLS (RLS is not
    -- exercised here), but the role name must exist for OWNER TO saebooks
    -- statements in migrations 0084+ to succeed.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks') THEN
        CREATE ROLE saebooks LOGIN SUPERUSER;
    END IF;

    -- ``saebooks_app`` — the runtime app role.  Migration 0056 creates
    -- it idempotently; this guard is belt-and-braces for partial
    -- migration replays.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
        CREATE ROLE saebooks_app
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            INHERIT
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            PASSWORD 'saebooks_app_test_pw';
    END IF;

    -- Grant saebooks_test (the Postgres superuser we boot with) the
    -- saebooks role so migrations running as saebooks_test can GRANT
    -- saebooks_app TO saebooks_test (which migration 0056 does via
    -- ``GRANT {_APP_ROLE} TO {current_role}``).
    IF NOT pg_has_role('saebooks_test', 'saebooks', 'member') THEN
        GRANT saebooks TO saebooks_test;
    END IF;
END
$$;
