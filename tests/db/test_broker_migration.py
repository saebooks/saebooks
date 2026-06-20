"""Broker alembic chain — up/down on an ephemeral DB (Phase 3b).

The broker has its OWN alembic chain (saebooks_group/migrations), independent of
the tenant chain, so the tenant CI ``alembic upgrade head`` does NOT cover it.
This test provisions its own ephemeral DB and proves ``0001_broker_init`` applies
(creating pair_registry + relay_log and NONE of the GL tables) and reverses
cleanly. Mirrors tests/db/test_migration_0159_ic_relay.py's ephemeral-DB idiom.

Postgres only.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from saebooks.config import settings as tenant_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

_FORBIDDEN_GL = {"accounts", "journal_entries", "journal_lines", "ic_txn", "ic_legs"}


def _admin_url(base_url: str, dbname: str) -> str:
    return make_url(base_url).set(database=dbname).render_as_string(hide_password=False)


def _run_broker_alembic(database_url: str, target: str) -> None:
    import saebooks_group.config as bcfg

    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(here, "saebooks_group", "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    prev = bcfg.settings.database_url
    bcfg.settings.database_url = database_url
    try:
        if target == "base":
            command.downgrade(cfg, "base")
        else:
            command.upgrade(cfg, target)
    finally:
        bcfg.settings.database_url = prev


async def _tables(url: str) -> set[str]:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            rows = (await conn.execute(sa.text(
                "SELECT relname FROM pg_class WHERE relkind='r' AND relname NOT LIKE 'pg_%' "
                "AND relname NOT LIKE 'sql_%' AND relname <> 'alembic_version'"
            ))).all()
        return {r.relname for r in rows}
    finally:
        await eng.dispose()


async def test_broker_chain_up_down() -> None:
    base_url = tenant_settings.database_url
    tmp_db = f"sb_broker_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(_admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{tmp_db}"'))
    finally:
        await admin.dispose()
    tmp_url = _admin_url(base_url, tmp_db)
    try:
        await asyncio.to_thread(_run_broker_alembic, tmp_url, "head")
        tables = await _tables(tmp_url)
        assert tables == {"pair_registry", "relay_log"}, (
            f"broker schema must be exactly pair_registry + relay_log, got {tables}"
        )
        assert not (tables & _FORBIDDEN_GL), "broker schema leaked GL tables"

        await asyncio.to_thread(_run_broker_alembic, tmp_url, "base")
        after = await _tables(tmp_url)
        assert not ({"pair_registry", "relay_log"} & after), (
            f"broker downgrade left tables: {after}"
        )
    finally:
        admin = create_async_engine(_admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": tmp_db})
                await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{tmp_db}"'))
        finally:
            await admin.dispose()
