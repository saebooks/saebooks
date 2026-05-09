"""Alembic environment for the multi-jurisdiction reference DB.

Mirrors ``alembic/env.py`` but points at REFERENCE_MIGRATION_DATABASE_URL
and uses ``ReferenceBase.metadata``. Refuses to run if the env var is
unset rather than silently using the company-DB URL — accidentally
applying reference DDL to the company DB would be a very bad day.
"""
import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from saebooks.config import settings as app_settings
from saebooks.db import ReferenceBase

config = context.config

_url = app_settings.reference_migration_database_url or os.environ.get(
    "REFERENCE_MIGRATION_DATABASE_URL", ""
)
if not _url:
    raise RuntimeError(
        "REFERENCE_MIGRATION_DATABASE_URL is not set. The reference DB "
        "alembic env refuses to fall back to DATABASE_URL — that would "
        "apply jurisdiction DDL to a company database. Set the env var "
        "to a saebooks_reference owner DSN and try again."
    )
config.set_main_option("sqlalchemy.url", _url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import every reference model so they register on ReferenceBase.metadata.
import saebooks.models.reference  # noqa: E402, F401

target_metadata = ReferenceBase.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
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
