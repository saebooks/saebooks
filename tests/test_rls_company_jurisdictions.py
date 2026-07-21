"""RLS coverage for ``company_jurisdictions`` (migration 0201,
M1.5 · 5-SUBJURIS).

0201 creates the company ↔ jurisdiction m2m with the non-negotiable
new-table RLS checklist. Proves:

1. relrowsecurity + relforcerowsecurity = t on the table.
2. A tenant_isolation policy keyed on tenant_id exists.
3. A NOBYPASSRLS saebooks_app session scoped to tenant A reads only
   tenant A's membership rows; tenant B's row is invisible across the
   boundary.
4. No app.current_tenant set => zero rows (deny by default).
5. The (company_id, jurisdiction_code) natural key rejects duplicates.

Reuses the saebooks_app role-flip engine pattern from
tests/test_rls_multijurisdiction.py (0133).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
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
from saebooks.models.company_jurisdiction import CompanyJurisdiction
from saebooks.models.tenant import Tenant
from tests.conftest import owner_seed_session

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"
_TABLE = "company_jurisdictions"


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
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            membership_id = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"RLS201-{label}-{suffix}",
                    slug=f"rls201-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RLS201-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            session.add(
                CompanyJurisdiction(
                    id=membership_id,
                    company_id=cid,
                    tenant_id=tid,
                    jurisdiction_code="AU-QLD",
                )
            )
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "membership_id": membership_id,
            }
            await session.flush()
        await session.commit()
    yield out
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE id = :mid"),
                {"mid": row["membership_id"]},
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


async def test_table_has_rls_enabled() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :name"
                ),
                {"name": _TABLE},
            )
        ).first()
    assert row is not None, f"{_TABLE} not present in pg_class"
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        f"RLS not fully enabled on {_TABLE} — migration 0201 failed or "
        "rolled back"
    )


async def test_table_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE tablename = :name "
                    "AND policyname = 'tenant_isolation'"
                ),
                {"name": _TABLE},
            )
        ).first()
    assert row is not None, (
        f"{_TABLE} missing tenant_isolation policy — 0201 incomplete"
    )
    for clause in (row.qual, row.with_check):
        assert clause and "tenant_id" in clause and "current_setting" in clause, (
            f"{_TABLE} policy is not the standard tenant_id predicate: "
            f"{clause!r}"
        )


async def test_membership_visible_to_own_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
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
                text(f"SELECT id FROM {_TABLE} WHERE id = :mid"),
                {"mid": a["membership_id"]},
            )
        ).all()
    assert len(visible) == 1, (
        f"tenant A could not see its own membership {a['membership_id']} — "
        "RLS predicate too tight"
    )


async def test_membership_invisible_across_tenants(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a["tenant_id"])},
        )
        leaked = (
            await session.execute(
                text(f"SELECT id FROM {_TABLE} WHERE id = :mid"),
                {"mid": b["membership_id"]},
            )
        ).all()
    assert leaked == [], (
        f"tenant A can read tenant B's membership {b['membership_id']} — "
        "RLS breach"
    )


async def test_no_tenant_context_sees_nothing(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    ids = [str(seeded[label]["membership_id"]) for label in ("tenant_a", "tenant_b")]
    async with AppSession() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    f"SELECT id FROM {_TABLE} "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": ids},
            )
        ).all()
    assert rows == [], (
        "unscoped saebooks_app session can read membership rows — "
        "deny-by-default failed"
    )


async def test_natural_key_rejects_duplicate(seeded: dict[str, Any]) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    a = seeded["tenant_a"]
    async with owner_seed_session() as session:
        session.add(
            CompanyJurisdiction(
                company_id=a["company_id"],
                tenant_id=a["tenant_id"],
                jurisdiction_code="AU-QLD",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.commit()
        await session.rollback()
