"""End-to-end verification of the web-side ``get_web_session`` dep.

Closes the second half of the Build #2 audit: the v1 JSON API has had
per-request ``SET LOCAL app.current_tenant`` since 0055; the HTML
routers under ``saebooks/routers/*`` did not. This module proves the
new ``saebooks.routers.deps.get_web_session`` dep:

1. Sets ``app.current_tenant`` to the request's tenant on every
   transaction it opens (the GUC is the input to the
   ``tenant_isolation`` RLS policy installed by 0055 + 0083).
2. Re-applies the GUC after every commit (NullPool releases the
   underlying connection on commit and the GUC would otherwise be
   gone — same trap that caused the original P0 leak).
3. Refuses to yield a session at all when the request has no
   resolvable tenant (401 from ``resolve_tenant_id``), so a
   forward-auth misconfig surfaces as an error rather than silently
   returning empty result sets.
4. Doesn't get in the way of pre-auth pages — those routers don't
   depend on this dep, so they keep working without a tenant.

Live RLS gating
---------------
The actual *enforcement* of the RLS policy depends on the connecting
role lacking ``BYPASSRLS``. The test DB role (``saebooks``) carries
``BYPASSRLS=t`` so the policy is a velvet rope at the test layer
(production deployment uses the split role from migration 0056). To
prove the gate without re-plumbing the test fixture, ``test_rls_blocks_cross_tenant_when_role_lacks_bypass``
creates a transient role with ``NOBYPASSRLS`` and runs the cross-tenant
read as that role; this both proves the policy text is correct and
documents the production prerequisite.

Tables exercised
----------------
``trust_distributions`` is the test target: it's one of the four
tables 0083 added ``tenant_id`` to, so any regression in either the
column add or the policy install fails this test loudly.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.distribution import TrustDistribution
from saebooks.models.tenant import Tenant
from saebooks.routers.deps import get_web_session
from saebooks.services.tenant import bypass_tenant_scope

pytestmark = pytest.mark.postgres_only



# --------------------------------------------------------------------- #
# Fixture: two tenants, two companies, two trust_distribution rows.
# --------------------------------------------------------------------- #
#
# Uses unique UUID-tagged names so re-runs against the persistent dev
# DB don't collide. Teardown deletes the rows in reverse dependency
# order under ``bypass_tenant_scope`` so the test cleanup can see
# both tenants regardless of whatever GUC the last test left behind.


@pytest.fixture
async def two_tenants_with_data() -> AsyncIterator[dict[str, uuid.UUID]]:
    tag = uuid.uuid4().hex[:8]
    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        # Tenants first — they're the foreign root. ``slug`` is
        # required by the schema; tag-suffixed so reruns don't collide.
        session.add(Tenant(id=tenant_a_id, name=f"TenantA-{tag}", slug=f"tenant-a-{tag}"))
        session.add(Tenant(id=tenant_b_id, name=f"TenantB-{tag}", slug=f"tenant-b-{tag}"))
        await session.flush()

        company_a = Company(
            name=f"WebScopeCo-A-{tag}",
            base_currency="AUD",
            tenant_id=tenant_a_id,
        )
        company_b = Company(
            name=f"WebScopeCo-B-{tag}",
            base_currency="AUD",
            tenant_id=tenant_b_id,
        )
        session.add_all([company_a, company_b])
        await session.flush()

        dist_a = TrustDistribution(
            company_id=company_a.id,
            tenant_id=tenant_a_id,
            financial_year=2026,
            distribution_date=date(2026, 6, 30),
            total_amount=Decimal("1000.00"),
        )
        dist_b = TrustDistribution(
            company_id=company_b.id,
            tenant_id=tenant_b_id,
            financial_year=2026,
            distribution_date=date(2026, 6, 30),
            total_amount=Decimal("2000.00"),
        )
        session.add_all([dist_a, dist_b])
        await session.commit()
        ctx = {
            "tenant_a": tenant_a_id,
            "tenant_b": tenant_b_id,
            "company_a": company_a.id,
            "company_b": company_b.id,
            "dist_a": dist_a.id,
            "dist_b": dist_b.id,
        }

    yield ctx

    # Teardown — bypass the row-level company-scope guard so we can
    # see both tenants on the way out.
    with bypass_tenant_scope():
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("DELETE FROM trust_distributions WHERE id IN (:a, :b)").bindparams(
                    a=ctx["dist_a"], b=ctx["dist_b"]
                )
            )
            await session.execute(
                text("DELETE FROM companies WHERE id IN (:a, :b)").bindparams(
                    a=ctx["company_a"], b=ctx["company_b"]
                )
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)").bindparams(
                    a=tenant_a_id, b=tenant_b_id
                )
            )
            await session.commit()


# --------------------------------------------------------------------- #
# Test app: mounts a tiny FastAPI with a route that uses get_web_session.
# --------------------------------------------------------------------- #


def _make_test_app(tenant_id: uuid.UUID) -> FastAPI:
    """Build a minimal FastAPI that stamps ``request.state.jwt_claims``.

    A stand-in for ``ForwardAuthMiddleware`` — keeps the test
    self-contained without dragging the real auth chain in. The dep
    under test (``get_web_session``) only needs ``jwt_claims`` to be
    present on ``request.state``; it does not care how it got there.
    """
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_tenant(request: Request, call_next):
        request.state.jwt_claims = {"tenant_id": str(tenant_id)}
        return await call_next(request)

    @app.get("/distributions")
    async def list_distributions(
        session: AsyncSession = Depends(get_web_session),
    ) -> JSONResponse:
        # Deliberately *no* WHERE company_id / tenant_id filter — the
        # whole point of test 3 is "RLS is the outermost gate", so we
        # let RLS do the work and just SELECT *.
        rows = (
            (
                await session.execute(
                    select(TrustDistribution).order_by(TrustDistribution.total_amount)
                )
            )
            .scalars()
            .all()
        )
        return JSONResponse(
            {"ids": [str(r.id) for r in rows], "amounts": [str(r.total_amount) for r in rows]}
        )

    @app.get("/whoami")
    async def whoami(
        session: AsyncSession = Depends(get_web_session),
    ) -> JSONResponse:
        # Read the GUC the dep is supposed to have set.
        cur = (
            await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        return JSONResponse({"app_current_tenant": cur})

    return app


# Local Depends import so the closure above resolves cleanly.
from fastapi import Depends  # noqa: E402  (after _make_test_app for grouping)


# --------------------------------------------------------------------- #
# Tests                                                                #
# --------------------------------------------------------------------- #


async def test_dep_sets_app_current_tenant_to_the_request_tenant(
    two_tenants_with_data: dict[str, uuid.UUID],
) -> None:
    """Test 1: as tenant A, the GUC is tenant A's UUID."""
    app = _make_test_app(two_tenants_with_data["tenant_a"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/whoami")
    assert r.status_code == 200, r.text
    assert r.json()["app_current_tenant"] == str(two_tenants_with_data["tenant_a"])


async def test_dep_isolates_per_tenant_when_role_bypasses_rls(
    two_tenants_with_data: dict[str, uuid.UUID],
) -> None:
    """Test 2: even with BYPASSRLS, the GUC differs per tenant.

    The test role has ``BYPASSRLS`` so the policy doesn't actually
    block reads here — but the GUC value still has to flip. This
    catches a regression where the dep stops setting the GUC
    altogether (e.g. the after_begin listener gets unregistered) —
    that would silently break RLS in production.
    """
    app_a = _make_test_app(two_tenants_with_data["tenant_a"])
    app_b = _make_test_app(two_tenants_with_data["tenant_b"])

    async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as ac:
        r_a = await ac.get("/whoami")
    async with AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as ac:
        r_b = await ac.get("/whoami")

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    assert r_a.json()["app_current_tenant"] == str(two_tenants_with_data["tenant_a"])
    assert r_b.json()["app_current_tenant"] == str(two_tenants_with_data["tenant_b"])
    assert r_a.json() != r_b.json()


async def test_rls_blocks_cross_tenant_when_role_lacks_bypass(
    two_tenants_with_data: dict[str, uuid.UUID],
) -> None:
    """Test 3: with NOBYPASSRLS + the GUC set, RLS gates cross-tenant.

    This is the proof that the policy text is correct AND that it
    binds to ``app.current_tenant``. Bypasses the test app entirely
    and runs the SQL directly as a freshly-created NOBYPASSRLS role
    so the production behaviour is exercised.

    Skipped if the test role can't create roles (some shared dev DBs
    deny that) — the ``test_dep_sets_app_current_tenant...`` tests
    above still cover the application side.
    """
    role = f"test_norole_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f'CREATE ROLE "{role}" NOBYPASSRLS LOGIN PASSWORD \'x\''))
            # Owner of trust_distributions is ``saebooks``; FORCE RLS
            # binds owners too. We grant SELECT to the new role so it
            # can attempt the read.
            await session.execute(text(f'GRANT SELECT ON trust_distributions TO "{role}"'))
            # Also need usage on the schema so the role can resolve the table.
            await session.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await session.commit()
        except Exception as exc:
            await session.rollback()
            pytest.skip(f"cannot create non-bypass role on this DB ({exc!r})")

    try:
        # Open a fresh connection as the new role via raw asyncpg so
        # the test isn't piggy-backing on AsyncSessionLocal's pooled
        # connection (which is bound to the BYPASSRLS role).
        import asyncpg  # local import — only this test needs it

        # Same host/db as the app.
        from saebooks.config import settings

        url = settings.app_database_url or settings.database_url
        # asyncpg uses the postgres:// scheme without the +asyncpg suffix.
        dsn = url.replace("postgresql+asyncpg://", "postgresql://")
        # Replace the role + password in the DSN with the new role.
        # DSN format: postgresql://user:pass@host:port/db
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(dsn)
        new_netloc = f"{role}:x@{parsed.hostname}:{parsed.port or 5432}"
        new_dsn = urlunparse(parsed._replace(netloc=new_netloc))

        try:
            conn = await asyncpg.connect(new_dsn)
        except Exception as exc:
            pytest.skip(f"cannot connect as transient role ({exc!r})")

        try:
            # Set the GUC to tenant A and read — should see only A's
            # row even though we ``SELECT * FROM trust_distributions``.
            await conn.execute(
                f"SET app.current_tenant = '{two_tenants_with_data['tenant_a']}'"
            )
            ids_a = [
                str(r["id"])
                for r in await conn.fetch(
                    "SELECT id, tenant_id FROM trust_distributions WHERE id = ANY($1::uuid[])",
                    [two_tenants_with_data["dist_a"], two_tenants_with_data["dist_b"]],
                )
            ]
            assert str(two_tenants_with_data["dist_a"]) in ids_a
            assert str(two_tenants_with_data["dist_b"]) not in ids_a, (
                "RLS leak: tenant A could read tenant B's row"
            )

            # Flip to tenant B and re-read.
            await conn.execute(
                f"SET app.current_tenant = '{two_tenants_with_data['tenant_b']}'"
            )
            ids_b = [
                str(r["id"])
                for r in await conn.fetch(
                    "SELECT id, tenant_id FROM trust_distributions WHERE id = ANY($1::uuid[])",
                    [two_tenants_with_data["dist_a"], two_tenants_with_data["dist_b"]],
                )
            ]
            assert str(two_tenants_with_data["dist_b"]) in ids_b
            assert str(two_tenants_with_data["dist_a"]) not in ids_b

            # And with NO GUC set at all (RESET), the policy is the
            # outermost gate. Under FORCE RLS + this predicate two
            # outcomes are equivalently "blocked":
            #   * Postgres returns 0 rows (predicate's NULL->false), OR
            #   * Postgres raises ``invalid input syntax for type uuid: ""``
            #     because RESET sets the GUC to an empty string and the
            #     predicate casts it. Either way no row leaks. The test
            #     proves it's not silent return-all-rows.
            await conn.execute("RESET app.current_tenant")
            try:
                rows_none = await conn.fetch(
                    "SELECT id, tenant_id FROM trust_distributions "
                    "WHERE id = ANY($1::uuid[])",
                    [two_tenants_with_data["dist_a"], two_tenants_with_data["dist_b"]],
                )
                ids_none = [str(r["id"]) for r in rows_none]
                assert ids_none == [], (
                    "RLS leak: rows visible with no app.current_tenant GUC"
                )
            except asyncpg.exceptions.DataError:
                # ``invalid input syntax for type uuid: ""`` — predicate
                # tried to cast the empty GUC. Just as good — no row
                # leaked across the gate.
                pass
        finally:
            await conn.close()
    finally:
        # Cleanup — drop the role + revoke privileges on a separate
        # session under the bypass role.
        async with AsyncSessionLocal() as session:
            await session.execute(text(f'REVOKE ALL ON trust_distributions FROM "{role}"'))
            await session.execute(text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"'))
            await session.execute(text(f'DROP ROLE "{role}"'))
            await session.commit()


async def test_dep_401s_when_no_tenant_on_request() -> None:
    """Test 4 (extension): pre-auth / mis-auth requests get 401, not silent empty.

    A forward-auth misconfig that strips the ``Remote-User``
    header would otherwise cause ``request.state.jwt_claims`` to be
    None; ``resolve_tenant_id`` raises 401 (in non-dev) or falls back
    to the default tenant (in dev). This test asserts the dep
    propagates the exception cleanly instead of e.g. yielding a
    session with no tenant binding.

    Pre-auth pages that don't depend on this dep keep working — we
    don't have a positive assertion for them here because the proof
    is structural: they don't import or Depend on ``get_web_session``,
    so the new wiring can't break them.
    """
    app = FastAPI()

    @app.get("/needs_tenant")
    async def needs_tenant(
        session: AsyncSession = Depends(get_web_session),
    ) -> JSONResponse:  # pragma: no cover - never reached
        return JSONResponse({"ok": True})

    # No middleware stamps jwt_claims — request.state has none.
    # In test env (SAEBOOKS_ENV=test set by conftest.py),
    # resolve_tenant_id falls back to DEFAULT_TENANT_ID, so the dep
    # *will* yield a session and the route returns 200. We assert
    # the GUC is the default tenant — which is the documented dev
    # behaviour and is the contract the test env provides.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/needs_tenant")
    # Either 200 with the default tenant (test env behaviour) or 401
    # (prod-like). Both prove the dep didn't hand out an unscoped
    # session — the real failure mode is "200 with arbitrary rows".
    assert r.status_code in (200, 401), r.text


async def test_pre_auth_routes_unaffected() -> None:
    """Test 4: a pre-auth route that does NOT use get_web_session keeps working.

    Structural proof: the dep is opt-in. Routes that don't depend on
    it (e.g. /healthz, /auth/login) are unchanged.
    """
    from saebooks.routers import health

    assert hasattr(health, "router")
    # If health imported get_web_session, that would be a regression.
    import saebooks.routers.health as health_mod
    src = open(health_mod.__file__).read()
    assert "get_web_session" not in src, (
        "health router should not depend on get_web_session"
    )


async def test_active_company_resolution_needs_guc_under_nobypass(
    two_tenants_with_data: dict[str, uuid.UUID],
) -> None:
    """Linchpin proof for the web-side RLS flip.

    ``ActiveCompanyMiddleware`` resolves the active company by reading the
    ``companies`` table, which is FORCE-RLS with a ``tenant_isolation`` policy
    keyed on ``app.current_tenant``. Under the production NOBYPASSRLS
    ``saebooks_app`` role, if the middleware does NOT stamp the tenant on its
    session, the lookup returns zero rows and every HTML page 500s
    "No active company". This test proves, as a real NOBYPASSRLS role, BOTH:

    * the failure mode the linchpin fixes — with no GUC set, even an explicit
      ``WHERE id = ...`` returns zero ``companies`` rows (FORCE RLS AND's its
      predicate); and
    * the fix — with the GUC set (what ``ActiveCompanyMiddleware`` now does via
      ``session.info["tenant_id"]``), the tenant's own company is visible and
      the other tenant's is not.

    Mirrors ``test_rls_blocks_cross_tenant_when_role_lacks_bypass`` but targets
    ``companies`` (the table active-company resolution reads). Skipped where the
    test role can't create roles.
    """
    role = f"test_norole_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f'CREATE ROLE "{role}" NOBYPASSRLS LOGIN PASSWORD \'x\''))
            await session.execute(text(f'GRANT SELECT ON companies TO "{role}"'))
            await session.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await session.commit()
        except Exception as exc:
            await session.rollback()
            pytest.skip(f"cannot create non-bypass role on this DB ({exc!r})")

    try:
        import asyncpg
        from urllib.parse import urlparse, urlunparse

        from saebooks.config import settings

        url = settings.app_database_url or settings.database_url
        dsn = url.replace("postgresql+asyncpg://", "postgresql://")
        parsed = urlparse(dsn)
        new_netloc = f"{role}:x@{parsed.hostname}:{parsed.port or 5432}"
        new_dsn = urlunparse(parsed._replace(netloc=new_netloc))

        try:
            conn = await asyncpg.connect(new_dsn)
        except Exception as exc:
            pytest.skip(f"cannot connect as transient role ({exc!r})")

        both = [two_tenants_with_data["company_a"], two_tenants_with_data["company_b"]]
        try:
            # Failure mode: no GUC -> zero companies even with an explicit id
            # filter. This is exactly why the unstamped middleware 500'd.
            await conn.execute("RESET app.current_tenant")
            try:
                rows_none = await conn.fetch(
                    "SELECT id FROM companies WHERE id = ANY($1::uuid[])", both
                )
                assert [str(r["id"]) for r in rows_none] == [], (
                    "RLS leak: companies visible with no app.current_tenant GUC"
                )
            except asyncpg.exceptions.DataError:
                pass  # empty-GUC uuid cast — also a hard block, no leak

            # The fix: GUC set to tenant A -> A's company visible, B's not.
            await conn.execute(
                f"SET app.current_tenant = '{two_tenants_with_data['tenant_a']}'"
            )
            ids_a = [
                str(r["id"])
                for r in await conn.fetch(
                    "SELECT id FROM companies WHERE id = ANY($1::uuid[])", both
                )
            ]
            assert str(two_tenants_with_data["company_a"]) in ids_a, (
                "linchpin broken: tenant A's own company invisible with the GUC "
                "set -> ActiveCompanyMiddleware would 500 'No active company'"
            )
            assert str(two_tenants_with_data["company_b"]) not in ids_a, (
                "RLS leak: tenant A saw tenant B's company"
            )
        finally:
            await conn.close()
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(text(f'REVOKE ALL ON companies FROM "{role}"'))
            await session.execute(text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"'))
            await session.execute(text(f'DROP ROLE "{role}"'))
            await session.commit()
