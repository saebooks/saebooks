"""Tests for the principal 'act as tenant' switch endpoint.

An authenticated principal calls ``/principal/act-as`` with a target tenant.
The server verifies an ACTIVE grant (the SECURITY DEFINER predicate) and only
then mints a tenant-bound session + binds ``app.current_tenant``. No grant ->
403, no token, no binding. A bound session's reads land in the bound tenant
ONLY, under the same FORCE-RLS as a native user.

We mint the principal session token directly here (login is covered in
test_principal_login.py) so these tests focus on the grant/binding gate.
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

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-act-as")

from sqlalchemy.pool import NullPool

from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.principal import (
    GrantStatus,
    Principal,
    PrincipalTenantGrant,
)
from saebooks.models.tenant import Tenant
from saebooks.services.principal import resolve_grant_role
from saebooks.services.principal_session import make_principal_token

# saebooks_app (NOBYPASSRLS) engine — same pattern as
# test_principal_cross_tenant.py / test_cross_tenant_isolation.py. The HTTP
# test API runs under the owner role (DATABASE_URL=saebooks_test), which does
# NOT enforce RLS isolation, so the *isolation* proof must run at the DB layer
# under the app role. The HTTP tests below prove the act-as GATING + binding
# wiring; this engine proves the binding actually isolates under FORCE-RLS.
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
    """Tenants A (granted), C (not granted), each with a marker contact, plus
    a principal granted only A."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"tenants": {}}
    async with owner_sessionmaker() as s:
        for label in ("A", "C"):
            tid, cid, contact_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            s.add(Tenant(id=tid, name=f"AA-{label}-{suffix}", slug=f"aa-{label}-{suffix}"))
            await s.flush()
            s.add(
                Company(
                    id=cid, tenant_id=tid, name=f"AA-Co-{label}-{suffix}",
                    base_currency="AUD", fin_year_start_month=7,
                )
            )
            await s.flush()
            s.add(
                Contact(
                    id=contact_id, tenant_id=tid, company_id=cid,
                    name=f"Marker-{label}-{suffix}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            await s.flush()
            out["tenants"][label] = {"tenant_id": tid, "contact_id": contact_id}

        p = Principal(
            id=uuid.uuid4(), display_name="ActAs Acct",
            username=f"actas-{suffix}",
        )
        s.add(p)
        await s.flush()
        grant = PrincipalTenantGrant(
            id=uuid.uuid4(), principal_id=p.id,
            tenant_id=out["tenants"]["A"]["tenant_id"],
            role="accountant", status=GrantStatus.ACTIVE.value,
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
            await s.execute(text("DELETE FROM contacts WHERE tenant_id=:t"), {"t": str(t["tenant_id"])})
            await s.execute(text("DELETE FROM companies WHERE tenant_id=:t"), {"t": str(t["tenant_id"])})
            await s.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": str(t["tenant_id"])})
        await s.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_act_as_granted_tenant_mints_bound_token(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    tok = make_principal_token(seeded["principal_id"])
    a = seeded["tenants"]["A"]
    resp = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(tok),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tenant_id"] == str(a["tenant_id"])
    assert data["role"] == "accountant"
    assert data["principal_id"] == str(seeded["principal_id"])
    assert data["access_token"]


async def test_act_as_non_granted_tenant_denied(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """act-as a tenant with no grant -> 403, no token."""
    tok = make_principal_token(seeded["principal_id"])
    c = seeded["tenants"]["C"]
    resp = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(c["tenant_id"])},
        headers=_auth(tok),
    )
    assert resp.status_code == 403, resp.text
    assert "access_token" not in resp.json()


async def test_bound_session_isolation_under_force_rls(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """The act-as binding isolates under FORCE-RLS (the saebooks_app role).

    This replicates EXACTLY what ``get_principal_tenant_session`` does — verify
    the grant via the SECURITY DEFINER predicate, then bind the tenant via
    ``session.info['tenant_id']`` (the after_begin listener issues the SET
    LOCAL) — but on the NOBYPASSRLS app engine where RLS actually fires. The
    bound read must return ONLY tenant A's contacts, never tenant C's.
    """
    pid = seeded["principal_id"]
    a = seeded["tenants"]["A"]

    async with app_sessionmaker() as s:
        # 1. grant re-verified (same call the dependency makes).
        async with s.begin():
            role = await resolve_grant_role(s, pid, a["tenant_id"])
            assert role == "accountant"
        # 2. bind exactly like the dependency, then read.
        s.info["tenant_id"] = str(a["tenant_id"])
        async with s.begin():
            rows = (
                await s.execute(
                    text("SELECT id, tenant_id FROM contacts")
                )
            ).all()
    assert rows, "expected to see tenant A's marker contact"
    for r in rows:
        assert str(r.tenant_id) == str(a["tenant_id"]), (
            "CROSS-TENANT LEAK: bound principal session saw a foreign tenant"
        )
    assert str(a["contact_id"]) in {str(r.id) for r in rows}


async def test_bound_session_http_wiring_returns_marker(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """HTTP smoke: a tenant-bound principal token reaches the bound-read
    endpoint (200) and sees the act-as tenant's marker contact, proving the
    act-as -> bound-token -> get_principal_tenant_session wiring is intact.

    NOTE: the test API runs under the owner role (DATABASE_URL=saebooks_test),
    which does not FORCE-isolate; the RLS isolation guarantee is proven in
    test_bound_session_isolation_under_force_rls above (app role). Here we only
    assert the binding path is wired and the marker is present.
    """
    tok = make_principal_token(seeded["principal_id"])
    a = seeded["tenants"]["A"]
    act = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(tok),
    )
    assert act.status_code == 200, act.text
    bound_token = act.json()["access_token"]

    resp = await client.get(
        "/api/v1/principal/acting/contacts", headers=_auth(bound_token)
    )
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()}
    assert str(a["contact_id"]) in ids


async def test_unbound_token_cannot_read_tenant_data(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    """An unbound login token cannot hit the bound-read endpoint -> 403."""
    tok = make_principal_token(seeded["principal_id"])
    resp = await client.get(
        "/api/v1/principal/acting/contacts", headers=_auth(tok)
    )
    assert resp.status_code == 403, resp.text


async def test_revoked_grant_blocks_act_as(
    client: AsyncClient, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """Revoke the grant -> a fresh act-as is 403 immediately."""
    tok = make_principal_token(seeded["principal_id"])
    a = seeded["tenants"]["A"]
    # Before: works.
    ok = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(tok),
    )
    assert ok.status_code == 200
    # Revoke.
    async with owner_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE principal_tenant_grants "
                "SET status='revoked', revoked_at=now() WHERE id=:id"
            ),
            {"id": str(seeded["grant_id"])},
        )
        await s.commit()
    # After: denied.
    denied = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(tok),
    )
    assert denied.status_code == 403, denied.text


async def test_revoked_grant_blocks_bound_session_reuse(
    client: AsyncClient, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """A previously-minted bound token stops working after revoke — the bound
    session re-verifies the grant on every request."""
    tok = make_principal_token(seeded["principal_id"])
    a = seeded["tenants"]["A"]
    act = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(tok),
    )
    bound_token = act.json()["access_token"]
    # Bound read works before revoke.
    pre = await client.get(
        "/api/v1/principal/acting/contacts", headers=_auth(bound_token)
    )
    assert pre.status_code == 200
    # Revoke.
    async with owner_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE principal_tenant_grants "
                "SET status='revoked', revoked_at=now() WHERE id=:id"
            ),
            {"id": str(seeded["grant_id"])},
        )
        await s.commit()
    # Same bound token now fails — grant re-checked per request.
    post = await client.get(
        "/api/v1/principal/acting/contacts", headers=_auth(bound_token)
    )
    assert post.status_code == 403, post.text


async def test_list_tenants_shows_only_own_grants(
    client: AsyncClient, seeded: dict[str, Any]
) -> None:
    tok = make_principal_token(seeded["principal_id"])
    resp = await client.get("/api/v1/principal/tenants", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    tids = {r["tenant_id"] for r in rows}
    assert tids == {str(seeded["tenants"]["A"]["tenant_id"])}
    assert all(r["role"] == "accountant" for r in rows)
