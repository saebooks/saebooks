"""RLS + search_path coverage after the pre-accounting schema move (0172).

Migration 0172 relocates the five pre-accounting tables
(``quotes``/``quote_lines``/``purchase_orders``/``purchase_order_lines``/
``time_entries``) from ``public`` into the ``preaccounting`` schema via
``ALTER TABLE ... SET SCHEMA``, and sets ``search_path = public,
preaccounting`` on the database + both connecting roles so the
schema-agnostic ORM keeps resolving unqualified names.

This module proves — LIVE, against a migrated Postgres — the two things
the runbook flagged as the correctness core of step 3:

1. The three tenant-scoped parents now live in ``preaccounting`` and STILL
   carry ``relrowsecurity`` + ``relforcerowsecurity`` and a
   ``tenant_isolation`` policy keyed on ``tenant_id`` (the policy survived
   ``SET SCHEMA`` — pg_policies naming/predicate intact).
2. A NOBYPASSRLS ``saebooks_app`` session:
   * resolves the moved table by its **unqualified** name (``FROM quotes``)
     — which ONLY works if ``search_path`` picked up ``preaccounting``, so
     this doubles as the search_path proof for the app role;
   * scoped to tenant A sees ONLY tenant A's quote — tenant B's row is
     invisible across the boundary (RLS still fires post-move);
   * with no ``app.current_tenant`` set, sees zero rows (deny by default).

Reuses the ``saebooks_app`` role-flip engine pattern from
``tests/test_rls_multijurisdiction.py`` (0133) /
``tests/services/bank_feeds/test_rls_bank_feed_accounts.py`` (0085).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

# NOTE: deliberately NOT ``saebooks.db.engine`` — that's the runtime
# engine, which IS the saebooks_app role under --rls (see
# docker-compose.test.yml). This file needs a connection that is always
# the real owner/superuser role (ALTER ROLE below requires it, and the
# catalog probes + URL-template below are clearer reading the one
# genuinely-fixed owner engine rather than a conditionally-app-role one).
from saebooks.db import _owner_role_engine as _owner_engine
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.quote import Quote
from saebooks.models.tenant import Tenant
from tests.conftest import owner_seed_session

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"
_PARENT_TABLES = ("quotes", "purchase_orders", "time_entries")
_MOVED_TABLES = (
    "quotes",
    "quote_lines",
    "purchase_orders",
    "purchase_order_lines",
    "time_entries",
)


def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="RLS + schemas are Postgres features; meaningless on SQLite.",
)


async def _ensure_app_role_login() -> bool:
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            return False
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
    return True


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            contact_id = uuid.uuid4()
            quote_id = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"RLS172-{label}-{suffix}",
                    slug=f"rls172-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RLS172-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            session.add(
                Contact(
                    id=contact_id,
                    company_id=cid,
                    tenant_id=tid,
                    name=f"RLS172-cust-{label}-{suffix}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            await session.flush()
            session.add(
                Quote(
                    id=quote_id,
                    tenant_id=tid,
                    company_id=cid,
                    number=f"Q172-{label}-{suffix}",
                    customer_id=contact_id,
                    issue_date=date(2026, 7, 1),
                )
            )
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "contact_id": contact_id,
                "quote_id": quote_id,
            }
            await session.flush()
        await session.commit()
    yield out
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            # Fully-qualified deletes: teardown runs on the owner engine
            # whose search_path also includes preaccounting, but be
            # explicit so this never depends on it.
            await session.execute(
                text("DELETE FROM preaccounting.quotes WHERE id = :qid"),
                {"qid": row["quote_id"]},
            )
            await session.execute(
                text("DELETE FROM contacts WHERE id = :cid"),
                {"cid": row["contact_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": row["tenant_id"]},
            )
        await session.commit()


async def test_moved_tables_live_in_preaccounting_schema() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'preaccounting' "
                    "AND tablename = ANY(:names)"
                ),
                {"names": list(_MOVED_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_MOVED_TABLES) - have
    assert not missing, (
        f"tables not in preaccounting schema after 0172: {missing}"
    )
    # And they must NOT still be in public.
    async with _owner_engine.connect() as conn:
        pub = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(_MOVED_TABLES)},
            )
        ).all()
    left_behind = {r.tablename for r in pub}
    assert not left_behind, (
        f"tables still present in public after SET SCHEMA: {left_behind}"
    )


async def test_parents_keep_force_rls_after_move() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'preaccounting' "
                    "AND c.relname = ANY(:names) ORDER BY c.relname"
                ),
                {"names": list(_PARENT_TABLES)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    missing = [t for t in _PARENT_TABLES if t not in state]
    assert not missing, f"parents not found in preaccounting: {missing}"
    bad = {t: v for t, v in state.items() if v != (True, True)}
    assert not bad, f"RLS not fully enabled after SET SCHEMA on {bad}"


async def test_parents_keep_tenant_isolation_policy_after_move() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, schemaname, qual FROM pg_policies "
                    "WHERE tablename = ANY(:names) "
                    "AND policyname = 'tenant_isolation' ORDER BY tablename"
                ),
                {"names": list(_PARENT_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_PARENT_TABLES) - have
    assert not missing, (
        f"tenant_isolation policy lost on {missing} after SET SCHEMA"
    )
    for r in rows:
        assert r.schemaname == "preaccounting", (
            f"{r.tablename} policy did not follow the table into "
            f"preaccounting (schemaname={r.schemaname})"
        )
        assert "tenant_id" in r.qual and "current_setting" in r.qual, (
            f"{r.tablename} policy predicate changed: {r.qual!r}"
        )


async def test_app_role_resolves_unqualified_moved_table(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Unqualified ``FROM quotes`` must resolve for saebooks_app.

    Proves search_path picked up ``preaccounting`` for the RLS-bound
    app role — if it had not, this SELECT would raise UndefinedTable.
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM quotes WHERE id = :qid"),
                {"qid": a["quote_id"]},
            )
        ).all()
    assert len(visible) == 1, (
        "tenant A could not see its own quote via unqualified name — "
        "search_path did not include preaccounting, or RLS too tight"
    )


async def test_quote_invisible_across_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_tenant = seeded["tenant_a"]["tenant_id"]
    b_quote = seeded["tenant_b"]["quote_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM quotes WHERE id = :qid"),
                {"qid": b_quote},
            )
        ).all()
    assert visible == [], (
        "CROSS-TENANT LEAK: tenant A saw tenant B's quote through the "
        "moved preaccounting.quotes table — RLS did not survive SET SCHEMA"
    )


async def test_no_tenant_set_denies_by_default(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_quote = seeded["tenant_a"]["quote_id"]
    async with AppSession() as session, session.begin():
        # Deliberately do NOT set app.current_tenant.
        visible = (
            await session.execute(
                text("SELECT id FROM quotes WHERE id = :qid"),
                {"qid": a_quote},
            )
        ).all()
    assert visible == [], (
        "quote visible with no app.current_tenant set — deny-by-default "
        "broke after the schema move"
    )
