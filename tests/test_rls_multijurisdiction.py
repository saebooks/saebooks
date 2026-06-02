"""RLS coverage for the three multi-jurisdiction tables migration 0133 closed.

0133 enables RLS + the standard ``tenant_isolation`` policy on
``tax_periods``, ``tax_returns`` and ``lodgement_records`` — Class-A tables
(direct ``tenant_id`` predicate) that 0100 created without RLS.

Proves (seeding ``tax_periods``):
1. relrowsecurity + relforcerowsecurity = t on all three tables.
2. A tenant_isolation policy keyed on tenant_id exists on all three.
3. A NOBYPASSRLS saebooks_app session scoped to tenant A reads only
   tenant A's tax_periods; tenant B's row is invisible across the boundary.
4. No app.current_tenant set => zero rows (deny by default).

Reuses the saebooks_app role-flip engine pattern from
tests/services/bank_feeds/test_rls_bank_feed_accounts.py (0085).
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

from saebooks.db import engine as _owner_engine
from saebooks.models.company import Company
from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
from saebooks.models.tenant import Tenant

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"
_TABLES = ("tax_periods", "tax_returns", "lodgement_records")


def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="RLS is a Postgres feature; meaningless on SQLite.",
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
    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            period_id = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"RLS133-{label}-{suffix}",
                    slug=f"rls133-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RLS133-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            session.add(
                TaxPeriod(
                    id=period_id,
                    company_id=cid,
                    tenant_id=tid,
                    jurisdiction="AUS",
                    period_type=TaxPeriodType.QUARTERLY,
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 3, 31),
                )
            )
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "period_id": period_id,
            }
            await session.flush()
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text("DELETE FROM tax_periods WHERE id = :pid"),
                {"pid": row["period_id"]},
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


async def test_three_tables_have_rls_enabled() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names) ORDER BY relname"
                ),
                {"names": list(_TABLES)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    missing = [t for t in _TABLES if t not in state]
    assert not missing, f"tables not present in pg_class: {missing}"
    bad = {t: v for t, v in state.items() if v != (True, True)}
    assert not bad, (
        f"RLS not fully enabled on {bad} — migration 0133 failed or rolled back"
    )


async def test_three_tables_have_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, qual FROM pg_policies "
                    "WHERE tablename = ANY(:names) "
                    "AND policyname = 'tenant_isolation' ORDER BY tablename"
                ),
                {"names": list(_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_TABLES) - have
    assert not missing, (
        f"tables missing tenant_isolation policy: {missing} — 0133 incomplete"
    )
    for r in rows:
        assert "tenant_id" in r.qual and "current_setting" in r.qual, (
            f"{r.tablename} policy is not the standard tenant_id predicate: "
            f"{r.qual!r}"
        )


async def test_tax_periods_visible_to_own_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    async with AppSession() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(a["tenant_id"])},
            )
            visible = (
                await session.execute(
                    text("SELECT id FROM tax_periods WHERE id = :pid"),
                    {"pid": a["period_id"]},
                )
            ).all()
    assert len(visible) == 1, (
        f"tenant A could not see its own tax_period {a['period_id']} — "
        f"RLS predicate too tight"
    )


async def test_tax_periods_invisible_across_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_tenant = seeded["tenant_a"]["tenant_id"]
    b_period = seeded["tenant_b"]["period_id"]
    async with AppSession() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(a_tenant)},
            )
            visible = (
                await session.execute(
                    text("SELECT id FROM tax_periods WHERE id = :pid"),
                    {"pid": b_period},
                )
            ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's tax_period {b_period} — the "
        f"tenant_isolation policy on tax_periods is broken or not FORCEd"
    )


async def test_tax_periods_no_tenant_set_returns_zero(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with AppSession() as session:
        async with session.begin():
            rows = (
                await session.execute(text("SELECT count(*) FROM tax_periods"))
            ).scalar_one()
    assert rows == 0, (
        f"expected 0 visible tax_periods with no tenant set, got {rows} — "
        f"RLS is not denying by default"
    )
