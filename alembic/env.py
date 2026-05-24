"""Alembic environment.

Postgres path: standard async alembic — runs the full migration tree
at ``alembic upgrade head``. This is the canonical production / CI
path; no change in semantics from the pre-SQLite-work behaviour.

SQLite path: alembic short-circuits and instead calls
``saebooks.db.bootstrap_schema`` on the configured engine. The
Postgres migration history is not portable to SQLite (RLS, sequences,
PG ENUM, ALTER TABLE forms beyond ADD COLUMN, etc) so we never try.
Cashbook on-device DBs are built from the ORM declarative metadata.

If you point ``alembic upgrade head`` at SQLite, you get the same
final schema as ``saebooks.db.bootstrap_schema`` — and alembic's
``version_num`` table is stamped to ``head`` so subsequent
``alembic current`` invocations still report a sensible state.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Side-effect import: registers @compiles hooks so postgresql.JSONB /
# postgresql.ARRAY usages inline in migrations render as JSON on the
# SQLite dialect. Cheap to keep on the Postgres path too — it's a no-op
# unless the dialect is sqlite.
from saebooks import db_types  # noqa: F401, E402
from saebooks.config import settings as app_settings
from saebooks.db import Base, bootstrap_schema

config = context.config
config.set_main_option("sqlalchemy.url", app_settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import models so they register with Base.metadata (Postgres
# autogenerate also needs this).
import saebooks.models  # noqa: E402, F401

target_metadata = Base.metadata


def _is_sqlite_url(url: str) -> bool:
    try:
        return make_url(url).get_backend_name() == "sqlite"
    except Exception:
        return url.startswith("sqlite")


def _configure_context(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )


def do_run_migrations(connection: Connection) -> None:
    _configure_context(connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("sqlalchemy.url not configured")
    if _is_sqlite_url(url):
        raise RuntimeError(
            "Offline alembic against SQLite is not supported — the "
            "Cashbook backend builds its schema via "
            "saebooks.db.bootstrap_schema, not migrations."
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _bootstrap_sqlite(database_url: str) -> None:
    """SQLite path — create_all from ORM metadata, then stamp alembic head.

    Stamping head means later ``alembic current`` calls don't fail
    with a "no version_num table" error, and operators inspecting the
    SQLite DB get a sensible result. We do NOT run any migration
    code (no upgrade_ops, no downgrade_ops — the migration tree is
    Postgres-only).
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(
        database_url,
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False},
    )
    try:
        await bootstrap_schema(eng)
        # stamp head — write the version_num table so subsequent
        # alembic invocations report a state. Done synchronously
        # using the underlying connection.
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        def _stamp(sync_conn: Connection) -> None:
            mc = MigrationContext.configure(sync_conn)
            script = ScriptDirectory.from_config(config)
            heads = script.get_heads()
            mc.stamp(script, "heads" if len(heads) != 1 else heads[0])

        async with eng.begin() as conn:
            await conn.run_sync(_stamp)
    finally:
        await eng.dispose()


async def run_migrations_online() -> None:
    cfg_section = config.get_section(config.config_ini_section, {})
    db_url = cfg_section.get("sqlalchemy.url", app_settings.database_url)
    if _is_sqlite_url(db_url):
        await _bootstrap_sqlite(db_url)
        return

    connectable = async_engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
