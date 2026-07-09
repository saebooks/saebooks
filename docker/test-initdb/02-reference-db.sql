-- Create the reference database for the multi-jurisdiction reference schema
-- (M1.5). The api test command migrates it via
-- ``alembic -c alembic_reference.ini upgrade head`` and points
-- REFERENCE_MIGRATION_DATABASE_URL / REFERENCE_DATABASE_URL at it (URLs
-- derived at runtime from DATABASE_URL, so no credentials live here).
--
-- Runs once on fresh volume init (the test stack nukes the volume with
-- ``down -v`` each run). Idempotent via \gexec guard for good measure.
SELECT 'CREATE DATABASE saebooks_reference_test OWNER saebooks'
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = 'saebooks_reference_test'
)\gexec
