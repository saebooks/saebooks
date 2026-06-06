"""Migration 0159 up/down reversibility on an ephemeral DB (Phase 3a).

Proves the new-table RLS checklist holds for ``ic_outbox`` / ``ic_inbox`` AND
that the migration is cleanly reversible (the standing reversibility working
rule for anything touching schema):

  1. ``alembic upgrade 0159`` from empty creates ``ic_outbox`` + ``ic_inbox``
     with ENABLE+FORCE RLS + a ``tenant_isolation`` policy, the REMOTE columns on
     ``ic_edges`` (with ``partner_company_id`` relaxed to nullable), and the
     idempotency/replay unique constraints;
  2. ``alembic downgrade 0158`` removes the two tables and the REMOTE columns and
     restores ``partner_company_id`` NOT NULL;
  3. ``alembic upgrade 0159`` again re-applies cleanly (idempotent shape — no
     leftover policy/trigger collision).

Self-contained: provisions + tears down its OWN ephemeral database, so it never
touches the shared session-migrated test DB. Mirrors
``tests/db/test_migration_0152_pending_trigger.py``'s ephemeral-DB idiom.
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
from saebooks import config as saebooks_config
from saebooks.config import settings

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

_REV_0159 = "0159_ic_remote_relay"
_REV_0158 = "0158_reclassifications"
_NEW_TABLES = ("ic_outbox", "ic_inbox")
_REMOTE_COLS = {
    "topology",
    "partner_tenant_id",
    "partner_endpoint",
    "relay_pubkey",
    "relay_privkey_ciphertext",
    "relay_token_prefix",
    "relay_token_hash",
    "relay_status",
    "authorised_by_principal_id",
}


def _admin_url(base_url: str, dbname: str) -> str:
    return make_url(base_url).set(database=dbname).render_as_string(
        hide_password=False
    )


def _run_alembic(database_url: str, target: str) -> None:
    """Drive alembic upgrade/downgrade to *target* against *database_url*."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = Config()  # no ini -> env.py skips fileConfig (logging pollution guard)
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)

    prev_env = os.environ.get("DATABASE_URL")
    prev_attr = saebooks_config.settings.database_url
    os.environ["DATABASE_URL"] = database_url
    saebooks_config.settings.database_url = database_url
    try:
        # alembic decides up vs down from the current vs target revision.
        command.upgrade(cfg, target)
    finally:
        saebooks_config.settings.database_url = prev_attr
        if prev_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_env


def _run_alembic_downgrade(database_url: str, target: str) -> None:
    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    prev_env = os.environ.get("DATABASE_URL")
    prev_attr = saebooks_config.settings.database_url
    os.environ["DATABASE_URL"] = database_url
    saebooks_config.settings.database_url = database_url
    try:
        command.downgrade(cfg, target)
    finally:
        saebooks_config.settings.database_url = prev_attr
        if prev_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_env


async def _tables_present(url: str) -> set[str]:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT relname FROM pg_class WHERE relname = ANY(:n)"
                    ),
                    {"n": list(_NEW_TABLES)},
                )
            ).all()
        return {r.relname for r in rows}
    finally:
        await eng.dispose()


async def _edge_cols(url: str) -> dict[str, str]:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT column_name, is_nullable FROM "
                        "information_schema.columns WHERE table_name = 'ic_edges'"
                    )
                )
            ).all()
        return {r.column_name: r.is_nullable for r in rows}
    finally:
        await eng.dispose()


async def _force_rls(url: str) -> dict[str, tuple[bool, bool]]:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname = ANY(:n)"
                    ),
                    {"n": list(_NEW_TABLES)},
                )
            ).all()
        return {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    finally:
        await eng.dispose()


async def test_0159_up_down_up_is_reversible() -> None:
    base_url = settings.database_url
    tmp_db = f"sb_mig0159_{uuid.uuid4().hex[:12]}"

    admin_engine = create_async_engine(
        _admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{tmp_db}"'))
    finally:
        await admin_engine.dispose()

    tmp_url = _admin_url(base_url, tmp_db)
    try:
        # 1. UP to 0159 from empty.
        await asyncio.to_thread(_run_alembic, tmp_url, _REV_0159)

        present = await _tables_present(tmp_url)
        assert set(_NEW_TABLES) <= present, (
            f"0159 upgrade did not create {set(_NEW_TABLES) - present}"
        )
        rls = await _force_rls(tmp_url)
        for t in _NEW_TABLES:
            assert rls.get(t) == (True, True), (
                f"{t} not ENABLE+FORCE RLS after 0159: {rls.get(t)}"
            )
        cols = await _edge_cols(tmp_url)
        assert set(cols) >= _REMOTE_COLS, (
            f"0159 did not add ic_edges REMOTE columns: {_REMOTE_COLS - set(cols)}"
        )
        assert cols.get("partner_company_id") == "YES", (
            "partner_company_id should be nullable after 0159 upgrade"
        )

        # 2. DOWN to 0158 — must remove everything 0159 added.
        await asyncio.to_thread(_run_alembic_downgrade, tmp_url, _REV_0158)

        present_after = await _tables_present(tmp_url)
        assert not (set(_NEW_TABLES) & present_after), (
            f"0159 downgrade left tables behind: {set(_NEW_TABLES) & present_after}"
        )
        cols_after = await _edge_cols(tmp_url)
        leftover = _REMOTE_COLS & set(cols_after)
        assert not leftover, f"0159 downgrade left ic_edges columns: {leftover}"
        assert cols_after.get("partner_company_id") == "NO", (
            "partner_company_id should be NOT NULL again after 0159 downgrade"
        )

        # 3. UP to 0159 again — re-applies cleanly (no policy/trigger collision).
        await asyncio.to_thread(_run_alembic, tmp_url, _REV_0159)
        present_again = await _tables_present(tmp_url)
        assert set(_NEW_TABLES) <= present_again, (
            "0159 re-upgrade after downgrade failed to recreate tables"
        )
    finally:
        admin_engine = create_async_engine(
            _admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(
                    sa.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :d AND pid <> pg_backend_pid()"
                    ),
                    {"d": tmp_db},
                )
                await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{tmp_db}"'))
        finally:
            await admin_engine.dispose()
