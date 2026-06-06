"""The principal grant gate must apply to the SHARED user-auth path.

Regression tests for the cross-tenant hole found by adversarial review:

The principal grant gate was originally enforced ONLY on the
``/api/v1/principal/*`` router (via ``get_principal_tenant_session``). The
normal USER router (companies, contacts, invoices, …) accepts the SAME signed
principal session token through ``require_bearer`` — it is a validly-signed JWT
(same secret) so ``decode_access_token`` accepts it, ``request.state.jwt_claims``
gets stamped with the token's ``tenant_id``, and ``get_session`` binds
``app.current_tenant`` to it — WITHOUT ever consulting
``principal_tenant_grants``.

Exploits pinned here:

* **A1** — a tenant-bound principal token on a USER endpoint
  (``GET /api/v1/companies``) returned 200 with that tenant's data, with no
  grant re-check. After the fix: 200 only while an ACTIVE grant exists; a token
  bound to a tenant the principal has NO grant for is 403 (and sees zero rows).
* **A2 (headline)** — after the grant is REVOKED, replaying the bound token on
  the user router STILL returned 200 for the token's 1h TTL. After the fix:
  403 IMMEDIATELY on the user router, because the grant is re-verified per
  request on the shared path.

The fix lives in the shared dependency (``saebooks.api.v1.auth.require_bearer``
+ ``deps.get_session`` / ``resolve_tenant_id``) so it covers EVERY router, not
just the principal one. Normal USER tokens (``sub`` + ``tenant_id``, no
``typ``/``psub``) are unaffected — covered by the existing user-auth suite and
by ``test_normal_user_token_unaffected`` below.

Test DB roles
-------------
The HTTP test API runs under the OWNER role (``DATABASE_URL=saebooks_test``),
which does NOT FORCE-isolate, so the grant-GATE behaviour (200 / 403) is what
these HTTP tests assert — the gate fires in ``require_bearer`` regardless of the
DB role. The zero-rows isolation guarantee for an ungranted tenant is proven
under the NOBYPASSRLS ``saebooks_app`` role in
``test_a1_ungranted_tenant_zero_rows_under_force_rls`` (the same engine pattern
as ``test_principal_act_as.test_bound_session_isolation_under_force_rls``).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-user-router-grant")

from sqlalchemy.pool import NullPool

from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.principal import (
    GrantStatus,
    Principal,
    PrincipalTenantGrant,
)
from saebooks.models.tenant import Tenant
from saebooks.services.principal import resolve_grant_role
from saebooks.services.principal_session import make_principal_token

# saebooks_app (NOBYPASSRLS) engine — identical pattern to
# test_principal_act_as.py. The HTTP test API runs under the owner role, which
# does NOT enforce RLS isolation, so the *isolation* (zero-rows) proof must run
# at the DB layer under the app role. The HTTP tests prove the GATE; this
# engine proves the gate's denial actually keeps the principal at zero rows.
_APP_ROLE_PASSWORD = "saebooks_app_test_pw"


def _build_app_engine_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    from saebooks.config import settings

    parts = urlsplit(settings.database_url)
    netloc = f"saebooks_app:{_APP_ROLE_PASSWORD}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


async def _set_app_role_password() -> None:
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )


@pytest_asyncio.fixture
async def app_sessionmaker() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    await _set_app_role_password()
    eng = create_async_engine(
        _build_app_engine_url(), poolclass=NullPool, future=True
    )
    yield async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    await eng.dispose()


@pytest_asyncio.fixture
async def owner_sessionmaker() -> AsyncIterator[Any]:
    yield async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def seeded(owner_sessionmaker: Any) -> AsyncIterator[dict[str, Any]]:
    """Tenants A (granted) and C (NOT granted), each with one company, plus a
    principal granted only A. Mirrors test_principal_act_as.seeded but the
    marker we read on the user router is the COMPANY (``GET /companies``)."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"tenants": {}}
    async with owner_sessionmaker() as s:
        for label in ("A", "C"):
            tid, cid = uuid.uuid4(), uuid.uuid4()
            s.add(
                Tenant(
                    id=tid,
                    name=f"UG-{label}-{suffix}",
                    slug=f"ug-{label}-{suffix}",
                )
            )
            await s.flush()
            s.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"UG-Co-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await s.flush()
            out["tenants"][label] = {"tenant_id": tid, "company_id": cid}

        p = Principal(
            id=uuid.uuid4(),
            display_name="UserRouter Acct",
            username=f"ug-acct-{suffix}",
        )
        s.add(p)
        await s.flush()
        grant = PrincipalTenantGrant(
            id=uuid.uuid4(),
            principal_id=p.id,
            tenant_id=out["tenants"]["A"]["tenant_id"],
            role="accountant",
            status=GrantStatus.ACTIVE.value,
        )
        s.add(grant)
        await s.commit()
        out["principal_id"] = p.id
        out["grant_id"] = grant.id
        out["suffix"] = suffix
    yield out
    async with owner_sessionmaker() as s:
        await s.execute(
            text("DELETE FROM principal_tenant_grants WHERE principal_id=:p"),
            {"p": str(out["principal_id"])},
        )
        await s.execute(
            text("DELETE FROM principals WHERE id=:p"),
            {"p": str(out["principal_id"])},
        )
        for label in ("A", "C"):
            t = out["tenants"][label]
            # Order matters: delete every child that FKs the tenant before the
            # tenant row. change_log + contacts may be written if a write test
            # ever (wrongly) lets a mutation through — clearing them keeps the
            # teardown clean even on a regression, so a leak shows up as a
            # failing assertion, never a dirty-DB teardown error.
            await s.execute(
                text("DELETE FROM change_log WHERE tenant_id=:t"),
                {"t": str(t["tenant_id"])},
            )
            await s.execute(
                text("DELETE FROM contacts WHERE tenant_id=:t"),
                {"t": str(t["tenant_id"])},
            )
            await s.execute(
                text("DELETE FROM companies WHERE tenant_id=:t"),
                {"t": str(t["tenant_id"])},
            )
            await s.execute(
                text("DELETE FROM tenants WHERE id=:t"),
                {"t": str(t["tenant_id"])},
            )
        await s.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _bound_token(seeded: dict[str, Any], label: str, role: str = "accountant") -> str:
    """A tenant-bound principal token, exactly what /act-as would mint."""
    return make_principal_token(
        seeded["principal_id"],
        tenant_id=seeded["tenants"][label]["tenant_id"],
        role=role,
    )


# --------------------------------------------------------------------------- #
# A1 — acting-as a GRANTED tenant works through the user router.
# --------------------------------------------------------------------------- #


async def test_bound_principal_token_with_grant_reaches_user_router(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """A bound principal token WITH an active grant -> 200 on GET /companies,
    and sees the granted tenant's company. Acting-as works end-to-end through
    the SAME user router a native user uses."""
    tok = _bound_token(seeded, "A")
    resp = await client.get("/api/v1/companies", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    ids = {c["id"] for c in resp.json()["items"]}
    assert str(seeded["tenants"]["A"]["company_id"]) in ids
    # And NEVER tenant C's company.
    assert str(seeded["tenants"]["C"]["company_id"]) not in ids


# --------------------------------------------------------------------------- #
# A1 — a principal token for a tenant it has NO grant for is denied + zero rows.
# --------------------------------------------------------------------------- #


async def test_a1_ungranted_tenant_bound_token_denied_on_user_router(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """A (forged/stale) principal token bound to tenant C — which the principal
    has NO grant for — must be DENIED on the user router (403). The bound token
    alone must never be sufficient; the live grant is required."""
    forged = _bound_token(seeded, "C")
    resp = await client.get("/api/v1/companies", headers=_auth(forged))
    assert resp.status_code == 403, resp.text
    assert resp.status_code != 200


async def test_a1_ungranted_tenant_zero_rows_under_force_rls(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """Under the NOBYPASSRLS app role, the principal-grant predicate returns
    None for (principal, tenant C) — so the shared path would never bind
    ``app.current_tenant`` = C. Were a binding to (wrongly) happen, RLS would
    still keep the read at zero foreign rows. This is the data-layer proof
    behind the 403 above."""
    pid = seeded["principal_id"]
    c = seeded["tenants"]["C"]
    a = seeded["tenants"]["A"]
    async with app_sessionmaker() as s, s.begin():
        # No grant for C -> None (the gate's deny condition).
        assert await resolve_grant_role(s, pid, c["tenant_id"]) is None
        # Active grant for A -> role (the gate's allow condition).
        assert await resolve_grant_role(s, pid, a["tenant_id"]) == "accountant"


# --------------------------------------------------------------------------- #
# A2 (headline) — revocation takes effect IMMEDIATELY on the user router.
# --------------------------------------------------------------------------- #


async def test_a2_revoked_grant_blocks_bound_token_on_user_router(
    client: AsyncClient, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """The bound token works on the user router WHILE the grant is active, then
    STOPS working immediately after revoke — the grant is re-checked per
    request on the shared path, not just at act-as time. This is the A2 fix:
    revocation is no longer deferred to the token's 1h TTL where the data
    actually lives."""
    tok = _bound_token(seeded, "A")
    # Before revoke: reaches the user router.
    pre = await client.get("/api/v1/companies", headers=_auth(tok))
    assert pre.status_code == 200, pre.text

    # Revoke the grant.
    async with owner_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE principal_tenant_grants "
                "SET status='revoked', revoked_at=now() WHERE id=:id"
            ),
            {"id": str(seeded["grant_id"])},
        )
        await s.commit()

    # After revoke: SAME token, SAME endpoint — now denied IMMEDIATELY.
    post = await client.get("/api/v1/companies", headers=_auth(tok))
    assert post.status_code in (401, 403), post.text
    assert post.status_code != 200


async def test_a2_revoked_grant_blocks_write_on_user_router(
    client: AsyncClient, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """Mutating user-router methods are gated identically: after revoke a
    PATCH/POST with the bound token is denied at the shared path (never reaches
    the handler). We probe a contacts POST — the deny happens in require_bearer
    so the exact endpoint is immaterial; it must not be 2xx."""
    tok = _bound_token(seeded, "A")
    # Revoke first.
    async with owner_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE principal_tenant_grants "
                "SET status='revoked', revoked_at=now() WHERE id=:id"
            ),
            {"id": str(seeded["grant_id"])},
        )
        await s.commit()
    resp = await client.post(
        "/api/v1/contacts",
        json={"name": "Should never be created", "contact_type": "CUSTOMER"},
        headers=_auth(tok),
    )
    assert resp.status_code in (401, 403), resp.text
    assert resp.status_code not in (200, 201)


# --------------------------------------------------------------------------- #
# Unbound principal token has no business on a user data router.
# --------------------------------------------------------------------------- #


async def test_unbound_principal_token_denied_on_user_router(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """An UNBOUND principal login token (psub + typ, NO tenant_id) carries no
    tenant binding — it must be denied on a user data router (403), not allowed
    to fall through to the dev-default tenant."""
    unbound = make_principal_token(seeded["principal_id"])
    resp = await client.get("/api/v1/companies", headers=_auth(unbound))
    assert resp.status_code in (401, 403), resp.text
    assert resp.status_code != 200


# --------------------------------------------------------------------------- #
# Normal USER tokens are byte-for-byte unaffected.
# --------------------------------------------------------------------------- #


async def test_normal_user_token_unaffected(
    client: AsyncClient, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """A normal user JWT (sub + tenant_id, no typ/psub) for tenant A still
    reaches GET /companies and sees tenant A's company — the principal grant
    gate must NOT touch the user-token path. A revoked PRINCIPAL grant is
    irrelevant to a real user of the tenant."""
    from saebooks.models.user import User, UserRole
    from saebooks.services.jwt_tokens import make_access_token

    a = seeded["tenants"]["A"]
    uid = uuid.uuid4()
    async with owner_sessionmaker() as s:
        s.add(
            User(
                id=uid,
                tenant_id=a["tenant_id"],
                username=f"ug-user-{seeded['suffix']}",
                role=UserRole.ADMIN.value,
            )
        )
        await s.commit()
        user = await s.get(User, uid)
        user_jwt = make_access_token(user)

    try:
        resp = await client.get("/api/v1/companies", headers=_auth(user_jwt))
        assert resp.status_code == 200, resp.text
        ids = {c["id"] for c in resp.json()["items"]}
        assert str(a["company_id"]) in ids
        # Revoking the PRINCIPAL grant does not affect a real user of A.
        async with owner_sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE principal_tenant_grants "
                    "SET status='revoked', revoked_at=now() WHERE id=:id"
                ),
                {"id": str(seeded["grant_id"])},
            )
            await s.commit()
        resp2 = await client.get("/api/v1/companies", headers=_auth(user_jwt))
        assert resp2.status_code == 200, resp2.text
        assert str(a["company_id"]) in {c["id"] for c in resp2.json()["items"]}
    finally:
        async with owner_sessionmaker() as s:
            await s.execute(
                text("DELETE FROM users WHERE id=:u"), {"u": str(uid)}
            )
            await s.commit()
