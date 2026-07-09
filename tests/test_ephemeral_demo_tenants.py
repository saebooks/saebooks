"""Ephemeral per-visit demo tenants — engine-side coverage.

Proves the four guarantees from the design spec:

1. provision() creates an isolated, SEEDED tenant whose JWT authenticates
   against /api/v1/auth/me and /api/v1/* RLS-scoped to its own company.
2. Two provisions are RLS-isolated — a cross-tenant probe under the
   NOBYPASSRLS saebooks_app role shows tenant A cannot read tenant B's rows.
3. The reaper hard-deletes ONLY ephemeral tenants (idle / aged) and leaves a
   real (non-demo) company untouched; the hard-delete is gated on
   ephemeral_demo_tenants membership.
4. The internal endpoint returns the login-shaped contract, enforces the
   per-IP rate-limit (429) and the global cap (503).

These are Postgres-only — RLS / FORCE RLS are meaningless on the SQLite
cashbook backend, and the reaper hard-delete relies on ON DELETE CASCADE FKs.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.config import settings
from saebooks.db import LoginSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant
from saebooks.services import ephemeral_demo

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="ephemeral demo tenants rely on Postgres RLS + cascade FKs.",
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _enable_demos():  # type: ignore[no-untyped-def]
    """Turn ephemeral demos on for the duration of each test, then restore."""
    prev = settings.demo_ephemeral_enabled
    settings.demo_ephemeral_enabled = True
    ephemeral_demo._reset_rate_limiter()
    yield
    settings.demo_ephemeral_enabled = prev
    ephemeral_demo._reset_rate_limiter()


def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


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


@pytest_asyncio.fixture
async def app_engine() -> AsyncIterator[Any]:
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def cleanup_demos() -> AsyncIterator[list[uuid.UUID]]:
    """Track provisioned demo company ids and hard-delete any survivors."""
    ids: list[uuid.UUID] = []
    yield ids
    for cid in ids:
        async with LoginSessionLocal() as session:
            try:
                await session.execute(text("SET LOCAL app.db_rebuild = 'on'"))
                await session.execute(
                    text("DELETE FROM companies WHERE id = :cid").bindparams(cid=cid)
                )
                await session.commit()
            except Exception:
                await session.rollback()
    # Drop any orphaned demo tenants (slug demo-*) with no remaining company.
    async with LoginSessionLocal() as session:
        try:
            await session.execute(
                text(
                    "DELETE FROM tenants WHERE slug LIKE 'demo-%' "
                    "AND id NOT IN (SELECT tenant_id FROM companies)"
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()


# --------------------------------------------------------------------------- #
# 1. provision creates an isolated, seeded tenant; its JWT works on /auth/me   #
# --------------------------------------------------------------------------- #


async def test_provision_creates_seeded_tenant_and_token_authenticates(
    cleanup_demos: list[uuid.UUID],
) -> None:
    result = await ephemeral_demo.provision(source_ip="203.0.113.10")
    cleanup_demos.append(result.company_id)

    assert result.tenant_id != uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert result.demo_user_email.startswith("demo+")
    assert result.access_token

    # Control row exists.
    async with LoginSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT company_id FROM ephemeral_demo_tenants "
                    "WHERE company_id = :cid"
                ).bindparams(cid=result.company_id)
            )
        ).first()
    assert row is not None, "control row not inserted"

    # Seeded: the company has AU accounts under its OWN tenant_id.
    async with LoginSessionLocal() as session:
        n_accts = (
            await session.execute(
                text(
                    "SELECT count(*) FROM accounts "
                    "WHERE company_id = :cid AND tenant_id = :tid"
                ).bindparams(cid=result.company_id, tid=result.tenant_id)
            )
        ).scalar_one()
    assert n_accts > 0, "no accounts seeded under the demo's tenant_id"

    # The minted JWT authenticates against /auth/me and resolves to the demo
    # user, scoped to the new tenant.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        me = await ac.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {result.access_token}"},
        )
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == result.demo_user_email
    assert body["tenant_id"] == str(result.tenant_id)


# --------------------------------------------------------------------------- #
# 2. Two provisions are RLS-isolated (cross-tenant probe).                     #
# --------------------------------------------------------------------------- #


async def test_two_provisions_are_rls_isolated(
    app_engine: Any, cleanup_demos: list[uuid.UUID]
) -> None:
    a = await ephemeral_demo.provision(source_ip="203.0.113.11")
    b = await ephemeral_demo.provision(source_ip="203.0.113.12")
    cleanup_demos.extend([a.company_id, b.company_id])
    assert a.tenant_id != b.tenant_id

    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    # Under tenant A's GUC, tenant B's accounts are invisible.
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a.tenant_id)},
        )
        own = (
            await session.execute(
                text("SELECT count(*) FROM accounts WHERE company_id = :cid"),
                {"cid": a.company_id},
            )
        ).scalar_one()
        leaked = (
            await session.execute(
                text("SELECT count(*) FROM accounts WHERE company_id = :cid"),
                {"cid": b.company_id},
            )
        ).scalar_one()
    assert own > 0, "tenant A cannot see its own accounts — RLS too tight"
    assert leaked == 0, (
        f"tenant A leaked tenant B's accounts ({leaked}) — demos not isolated"
    )


# --------------------------------------------------------------------------- #
# 3. Reaper deletes only ephemeral tenants; real company untouched.           #
# --------------------------------------------------------------------------- #


async def _make_real_company(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a non-demo tenant+company (NO ephemeral_demo_tenants row)."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    session.add(Tenant(id=tid, name=f"real-{suffix}", slug=f"real-{suffix}"))
    await session.flush()
    session.add(
        Company(id=cid, tenant_id=tid, name=f"Real Co {suffix}", base_currency="AUD")
    )
    await session.commit()
    return tid, cid


async def test_reaper_reaps_only_ephemeral_and_spares_real(
    cleanup_demos: list[uuid.UUID],
) -> None:
    # An idle demo (last_seen_at well past the idle TTL).
    idle = await ephemeral_demo.provision(source_ip="203.0.113.20")
    # A fresh demo (within TTL) — must survive the sweep.
    fresh = await ephemeral_demo.provision(source_ip="203.0.113.21")
    cleanup_demos.extend([idle.company_id, fresh.company_id])

    # A real company with NO control row — the reaper must never touch it.
    async with LoginSessionLocal() as session:
        real_tid, real_cid = await _make_real_company(session)

    try:
        # Backdate the idle demo's last_seen_at beyond the idle TTL.
        async with LoginSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE ephemeral_demo_tenants SET last_seen_at = :ts "
                    "WHERE company_id = :cid"
                ).bindparams(
                    ts=datetime.now(UTC)
                    - timedelta(seconds=settings.demo_idle_ttl + 60),
                    cid=idle.company_id,
                )
            )
            await session.commit()

        reaped = await ephemeral_demo.reap_once()

        assert idle.company_id in reaped, "idle demo was not reaped"
        assert fresh.company_id not in reaped, "fresh demo wrongly reaped"

        async with LoginSessionLocal() as session:
            idle_gone = (
                await session.execute(
                    text("SELECT count(*) FROM companies WHERE id = :cid").bindparams(
                        cid=idle.company_id
                    )
                )
            ).scalar_one()
            fresh_alive = (
                await session.execute(
                    text("SELECT count(*) FROM companies WHERE id = :cid").bindparams(
                        cid=fresh.company_id
                    )
                )
            ).scalar_one()
            real_alive = (
                await session.execute(
                    text("SELECT count(*) FROM companies WHERE id = :cid").bindparams(
                        cid=real_cid
                    )
                )
            ).scalar_one()
        assert idle_gone == 0, "idle demo company still present after reap"
        assert fresh_alive == 1, "fresh demo company wrongly deleted"
        assert real_alive == 1, "REAL company was deleted by the reaper — gate broken"
    finally:
        # Clean up the real company we created.
        async with LoginSessionLocal() as session:
            await session.execute(text("SET LOCAL app.db_rebuild = 'on'"))
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid").bindparams(cid=real_cid)
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=real_tid)
            )
            await session.commit()


async def test_hard_delete_refuses_non_demo_company() -> None:
    """The hard-delete helper refuses a company that is not an ephemeral demo."""
    async with LoginSessionLocal() as session:
        real_tid, real_cid = await _make_real_company(session)
    try:
        async with LoginSessionLocal() as session:
            with pytest.raises(ValueError):
                await ephemeral_demo._hard_delete_demo_company(session, real_cid)
        # Still there.
        async with LoginSessionLocal() as session:
            alive = (
                await session.execute(
                    text("SELECT count(*) FROM companies WHERE id = :cid").bindparams(
                        cid=real_cid
                    )
                )
            ).scalar_one()
        assert alive == 1
    finally:
        async with LoginSessionLocal() as session:
            await session.execute(text("SET LOCAL app.db_rebuild = 'on'"))
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid").bindparams(cid=real_cid)
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=real_tid)
            )
            await session.commit()


# --------------------------------------------------------------------------- #
# 4. Endpoint contract: shape, rate-limit (429), cap (503).                    #
# --------------------------------------------------------------------------- #


async def test_provision_endpoint_contract(
    cleanup_demos: list[uuid.UUID],
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/internal/demo/provision",
            json={"source_ip": "203.0.113.30"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    for key in (
        "access_token",
        "token_type",
        "expires_in",
        "company_id",
        "tenant_id",
        "demo_user_email",
    ):
        assert key in body, f"missing {key} in provision response"
    assert body["token_type"] == "bearer"
    cleanup_demos.append(uuid.UUID(body["company_id"]))

    # The endpoint-issued token authenticates the same as a login token.
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        me = await ac.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
    assert me.status_code == 200, me.text


async def test_provision_rate_limit_returns_429(
    cleanup_demos: list[uuid.UUID],
) -> None:
    prev = settings.demo_provision_per_ip_per_min
    settings.demo_provision_per_ip_per_min = 2
    ephemeral_demo._reset_rate_limiter()
    try:
        ip = "203.0.113.40"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r1 = await ac.post("/internal/demo/provision", json={"source_ip": ip})
            r2 = await ac.post("/internal/demo/provision", json={"source_ip": ip})
            r3 = await ac.post("/internal/demo/provision", json={"source_ip": ip})
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, r2.text
        assert r3.status_code == 429, f"expected 429, got {r3.status_code}: {r3.text}"
        assert r3.json()["error"] == "rate_limited"
        for r in (r1, r2):
            cleanup_demos.append(uuid.UUID(r.json()["company_id"]))
    finally:
        settings.demo_provision_per_ip_per_min = prev
        ephemeral_demo._reset_rate_limiter()


async def test_provision_cap_returns_503(
    cleanup_demos: list[uuid.UUID],
) -> None:
    """At cap with nothing reapable, provision returns 503 demo_at_capacity.

    We set the cap to the current live-demo count so the next provision is at
    cap. The only existing demo is fresh (not idle), so the reap-oldest step
    cannot free a slot, and the cap path returns 503.
    """
    # One fresh demo so there is something to be "at cap" with.
    seed = await ephemeral_demo.provision(source_ip="203.0.113.50")
    cleanup_demos.append(seed.company_id)

    async with LoginSessionLocal() as session:
        live = (
            await session.execute(
                text("SELECT count(*) FROM ephemeral_demo_tenants")
            )
        ).scalar_one()

    prev = settings.demo_max_tenants
    settings.demo_max_tenants = int(live)
    ephemeral_demo._reset_rate_limiter()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/internal/demo/provision", json={"source_ip": "203.0.113.51"}
            )
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"] == "demo_at_capacity"
    finally:
        settings.demo_max_tenants = prev
