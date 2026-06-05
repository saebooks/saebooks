"""Regression: migration 0152 must survive a journal_lines table WITH rows.

THE BUG (reproduced live on the sandbox tenant 2026-06-06)
----------------------------------------------------------
``0152_journal_company_structural`` upgrade() does, in one transaction:

    add company_id (nullable)
      -> backfill: UPDATE journal_lines SET company_id = parent.company_id
      -> ALTER ... SET NOT NULL
      -> composite FK + coherence triggers

journal_lines carries a DEFERRABLE INITIALLY DEFERRED *constraint trigger*
``trg_je_balance_jl`` (the posted-balance / has-lines guard). The mass
backfill UPDATE fires that trigger once per touched row, queuing a pending
trigger event for EVERY existing row. PostgreSQL then refuses the in-tx
``ALTER TABLE ... SET NOT NULL`` with:

    asyncpg.exceptions.ObjectInUseError: cannot ALTER TABLE "journal_lines"
    because it has pending trigger events

CI never caught it because the migration job runs ``alembic upgrade head``
against a FRESH EMPTY DB: no rows -> no backfill events -> no pending
events -> the ALTER succeeds. The bug only manifests on a DB that already
holds journal_lines rows — i.e. every real tenant.

THE FIX
-------
``op.execute("SET CONSTRAINTS ALL IMMEDIATE")`` right after the backfill
forces the deferred balance trigger to fire and drain before the ALTER.

THIS TEST
---------
Builds an ephemeral Postgres DB, migrates it to 0151 (pre-0152 state),
seeds a *balanced POSTED* journal entry with two journal_lines (so the
deferred balance trigger has real work to do on real rows — the exact
production scenario), then runs ``alembic upgrade head`` and asserts it
gets all the way to 0153 with company_id populated + NOT NULL + the new
FK/triggers in place. Before the fix this test fails at 0152 with
ObjectInUseError; after the fix it is green.

Self-contained: it provisions and tears down its OWN database, so it does
not touch the shared session-migrated test DB.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from saebooks import config as saebooks_config
from saebooks.config import settings

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

PRE_FIX_REVISION = "0151_stmt_templates"


def _alembic_head() -> str:
    """The current single alembic head, derived from the script directory.

    Derived (not hard-coded) so this regression test tracks the real
    migration head and never needs a manual bump when a new migration lands
    on top — a hard-coded head silently broke this test when
    0154_intercompany_phase1 advanced the chain past 0153.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"
    return heads[0]


HEAD_REVISION = _alembic_head()


def _admin_url(base_url: str, dbname: str) -> str:
    """Same server/credentials as base_url but pointed at *dbname*.

    Uses ``render_as_string(hide_password=False)`` rather than ``str(url)``:
    ``str()`` of a SQLAlchemy URL MASKS the password as ``***``, which would
    be sent verbatim and fail authentication.
    """
    return make_url(base_url).set(database=dbname).render_as_string(
        hide_password=False
    )


def _run_alembic_upgrade(database_url: str, target: str) -> None:
    """Drive alembic's programmatic upgrade against *database_url*.

    alembic/env.py reads ``saebooks.config.settings.database_url`` at env-load
    time (re-executed on every ``command.upgrade``). ``settings`` is a frozen
    pydantic-settings singleton, so merely setting ``DATABASE_URL`` in the
    environment is NOT enough — we must also override the already-materialised
    ``settings.database_url`` attribute so env.py targets the ephemeral DB.
    Both are restored in ``finally``.

    Runs synchronously; callers invoke it via ``asyncio.to_thread`` because
    env.py calls ``asyncio.run`` internally (cannot nest in the pytest loop).

    The ``Config`` is built WITHOUT pointing at ``alembic.ini``: env.py runs
    ``logging.config.fileConfig`` only when ``config_file_name`` is set, and
    fileConfig defaults to ``disable_existing_loggers=True``, which would mute
    the ``saebooks.db`` logger and break unrelated ``caplog``-based tests that
    run later in the same process (cross-test logging pollution). We only need
    ``script_location`` + ``sqlalchemy.url``, so we pass them directly and
    leave the process logging configuration untouched.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = Config()  # no ini file -> env.py skips fileConfig (see docstring)
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)

    prev_env = os.environ.get("DATABASE_URL")
    prev_attr = saebooks_config.settings.database_url
    os.environ["DATABASE_URL"] = database_url
    saebooks_config.settings.database_url = database_url
    try:
        command.upgrade(cfg, target)
    finally:
        saebooks_config.settings.database_url = prev_attr
        if prev_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_env


async def test_migration_0152_survives_existing_journal_lines() -> None:
    base_url = settings.database_url  # postgresql+asyncpg://.../<db>
    tmp_db = f"sb_mig0152_{uuid.uuid4().hex[:12]}"

    # asyncpg cannot run CREATE/DROP DATABASE inside a transaction; use the
    # AUTOCOMMIT isolation level on a connection to the maintenance db.
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
        # 1. Migrate the throwaway DB to the pre-0152 schema state. Run in a
        #    worker thread: alembic env.py calls asyncio.run() internally,
        #    which cannot nest inside the pytest-asyncio event loop.
        await asyncio.to_thread(
            _run_alembic_upgrade, tmp_url, PRE_FIX_REVISION
        )

        # 2. Seed a balanced POSTED entry with two lines, so the deferred
        #    balance trigger has real rows to validate on the 0152 backfill.
        engine = create_async_engine(tmp_url)
        tenant_id = uuid.uuid4()
        company_id = uuid.uuid4()
        acct_dr = uuid.uuid4()
        acct_cr = uuid.uuid4()
        entry_id = uuid.uuid4()
        try:
            async with engine.begin() as conn:
                # journal_entries / accounts have FORCE RLS keyed on the
                # app.current_tenant GUC. SET cannot take bind params;
                # set_config() can. (The test superuser bypasses RLS anyway,
                # but a non-super migration role would need this.)
                await conn.execute(
                    sa.text(
                        "SELECT set_config('app.current_tenant', :tid, false)"
                    ).bindparams(tid=str(tenant_id))
                )
                await conn.execute(
                    sa.text(
                        "INSERT INTO tenants (id, name, slug) "
                        "VALUES (:id, 'mig0152', :slug)"
                    ).bindparams(id=tenant_id, slug=f"mig0152-{tmp_db}")
                )
                await conn.execute(
                    sa.text(
                        "INSERT INTO companies "
                        "(id, name, version, tenant_id) "
                        "VALUES (:id, 'Co', 1, :tid)"
                    ).bindparams(id=company_id, tid=tenant_id)
                )
                for aid, code, atype in (
                    (acct_dr, "1000", "ASSET"),
                    (acct_cr, "4000", "INCOME"),
                ):
                    await conn.execute(
                        sa.text(
                            "INSERT INTO accounts "
                            "(id, company_id, code, name, account_type, "
                            " version, tenant_id) "
                            "VALUES (:id, :cid, :code, :code, "
                            "CAST(:atype AS account_type_enum), 1, :tid)"
                        ).bindparams(
                            id=aid,
                            cid=company_id,
                            code=code,
                            atype=atype,
                            tid=tenant_id,
                        )
                    )
                await conn.execute(
                    sa.text(
                        "INSERT INTO journal_entries "
                        "(id, company_id, ref, entry_date, status, "
                        " version, tenant_id) "
                        "VALUES (:id, :cid, 'JE-1', CURRENT_DATE, "
                        " 'POSTED', 1, :tid)"
                    ).bindparams(id=entry_id, cid=company_id, tid=tenant_id)
                )
                for lno, aid, dr, cr in (
                    (1, acct_dr, Decimal("100.00"), Decimal("0")),
                    (2, acct_cr, Decimal("0"), Decimal("100.00")),
                ):
                    await conn.execute(
                        sa.text(
                            "INSERT INTO journal_lines "
                            "(id, entry_id, line_no, account_id, debit, credit) "
                            "VALUES (:id, :eid, :lno, :aid, :dr, :cr)"
                        ).bindparams(
                            id=uuid.uuid4(),
                            eid=entry_id,
                            lno=lno,
                            aid=aid,
                            dr=dr,
                            cr=cr,
                        )
                    )
            # at COMMIT the deferred trg_je_balance_jl fires and passes
            # (balanced) — confirming the seed is a valid 0151 state.
        finally:
            await engine.dispose()

        # 3. THE ASSERTION UNDER TEST: upgrade past 0152 on a NON-empty
        #    journal_lines. Pre-fix this raises ObjectInUseError.
        await asyncio.to_thread(_run_alembic_upgrade, tmp_url, "head")

        # 4. Verify the end state is correct + reached head.
        engine = create_async_engine(tmp_url)
        try:
            async with engine.connect() as conn:
                ver = (
                    await conn.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                ).scalar_one()
                assert ver == HEAD_REVISION, ver

                # company_id backfilled to the parent entry's company + NOT NULL
                row = (
                    await conn.execute(
                        sa.text(
                            "SELECT company_id, "
                            "(SELECT bool_or(company_id IS NULL) "
                            " FROM journal_lines) AS any_null "
                            "FROM journal_lines LIMIT 1"
                        )
                    )
                ).one()
                assert str(row.company_id) == str(company_id), row.company_id
                assert row.any_null is False

                # column is NOT NULL
                notnull = (
                    await conn.execute(
                        sa.text(
                            "SELECT attnotnull FROM pg_attribute "
                            "WHERE attrelid = 'journal_lines'::regclass "
                            "AND attname = 'company_id'"
                        )
                    )
                ).scalar_one()
                assert notnull is True

                # composite FK + both 0152 triggers present
                fk = (
                    await conn.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_constraint "
                            "WHERE conname = 'fk_journal_lines_account_company'"
                        )
                    )
                ).scalar_one()
                assert fk == 1

                trigs = set(
                    (
                        await conn.execute(
                            sa.text(
                                "SELECT tgname FROM pg_trigger "
                                "WHERE tgrelid = 'journal_lines'::regclass "
                                "AND NOT tgisinternal"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert "trg_journal_lines_company" in trigs, trigs
                assert "trg_je_balance_jl" in trigs, trigs
        finally:
            await engine.dispose()
    finally:
        # Tear down the throwaway DB (terminate stragglers first).
        admin_engine = create_async_engine(
            _admin_url(base_url, "postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(
                    sa.text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity WHERE datname = :db"
                    ).bindparams(db=tmp_db)
                )
                await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{tmp_db}"'))
        finally:
            await admin_engine.dispose()
