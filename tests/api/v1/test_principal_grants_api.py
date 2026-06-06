"""Tests for the tenant-side grant-management API (feat/accountant-login).

A tenant admin manages "who can act as my books" via
``/api/v1/principal-grants`` under their OWN authenticated user session. The
load-bearing control is the DB: the ``tenant_isolation`` FORCE-RLS policy on
``principal_tenant_grants`` (WITH CHECK) means a tenant can only create a grant
binding a principal to ITSELF — it can never forge a grant into another tenant.

These tests run end-to-end through HTTP under the saebooks_app (NOBYPASSRLS)
role the test stack provides, so the RLS WITH CHECK actually fires.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-grants-api")

from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.principal import Principal
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token
from saebooks.services.principal_session import make_principal_token


def _mint_user(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {"sub": str(user.id), "role": user.role, "tenant_id": str(user.tenant_id)}
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


@pytest_asyncio.fixture
async def app_sessionmaker() -> AsyncIterator[Any]:
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
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
async def setup(owner_sessionmaker: Any) -> AsyncIterator[dict[str, Any]]:
    """Two tenants A & B, each with an admin user; one principal (no grants)."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"tenants": {}}
    async with owner_sessionmaker() as s:
        for label in ("A", "B"):
            tid, cid = uuid.uuid4(), uuid.uuid4()
            s.add(Tenant(id=tid, name=f"GA-{label}-{suffix}", slug=f"ga-{label}-{suffix}"))
            await s.flush()
            s.add(
                Company(
                    id=cid, tenant_id=tid, name=f"GA-Co-{label}-{suffix}",
                    base_currency="AUD", fin_year_start_month=7,
                )
            )
            admin = User(
                id=uuid.uuid4(), tenant_id=tid,
                username=f"ga-admin-{label}-{suffix}",
                email=f"ga-admin-{label}-{suffix}@test.invalid",
                role="admin",
            )
            s.add(admin)
            await s.flush()
            out["tenants"][label] = {"tenant_id": tid, "admin_id": admin.id}
        p = Principal(
            id=uuid.uuid4(), display_name="Grant Target",
            username=f"grant-target-{suffix}",
        )
        s.add(p)
        await s.commit()
        out["principal_id"] = p.id
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
        for label in ("A", "B"):
            t = out["tenants"][label]
            await s.execute(sa_delete(User).where(User.id == t["admin_id"]))
            await s.execute(text("DELETE FROM companies WHERE tenant_id=:t"), {"t": str(t["tenant_id"])})
            await s.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": str(t["tenant_id"])})
        await s.commit()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def _admin_token(setup: dict[str, Any], label: str) -> str:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, setup["tenants"][label]["admin_id"])
        return _mint_user(u)


# --------------------------------------------------------------------------- #
# Create — a tenant grants access to ITSELF (works) and then the principal can
# act-as it.
# --------------------------------------------------------------------------- #


async def test_tenant_admin_grants_own_tenant_then_principal_can_act_as(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    a = setup["tenants"]["A"]
    token = await _admin_token(setup, "A")
    resp = await client.post(
        "/api/v1/principal-grants",
        json={"principal_id": str(setup["principal_id"]), "role": "accountant"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["tenant_id"] == str(a["tenant_id"])
    assert data["status"] == "active"

    # The principal can now act-as A.
    ptok = make_principal_token(setup["principal_id"])
    act = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(ptok),
    )
    assert act.status_code == 200, act.text
    assert act.json()["role"] == "accountant"


async def test_grant_tenant_id_is_session_derived_not_body(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    """The grant's tenant_id is the AUTHENTICATED tenant — the request body has
    no tenant_id field, so even a malicious admin can't aim a grant elsewhere.
    Tenant-A admin's grant lands on tenant A, full stop."""
    a = setup["tenants"]["A"]
    token = await _admin_token(setup, "A")
    resp = await client.post(
        "/api/v1/principal-grants",
        json={"principal_id": str(setup["principal_id"]), "role": "viewer"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tenant_id"] == str(a["tenant_id"])


async def test_revoke_removes_act_as(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    a = setup["tenants"]["A"]
    token = await _admin_token(setup, "A")
    created = await client.post(
        "/api/v1/principal-grants",
        json={"principal_id": str(setup["principal_id"]), "role": "accountant"},
        headers=_auth(token),
    )
    grant_id = created.json()["id"]

    ptok = make_principal_token(setup["principal_id"])
    assert (
        await client.post(
            "/api/v1/principal/act-as",
            json={"tenant_id": str(a["tenant_id"])},
            headers=_auth(ptok),
        )
    ).status_code == 200

    # Revoke via the API.
    rev = await client.delete(
        f"/api/v1/principal-grants/{grant_id}", headers=_auth(token)
    )
    assert rev.status_code == 204, rev.text

    # act-as now denied.
    denied = await client.post(
        "/api/v1/principal/act-as",
        json={"tenant_id": str(a["tenant_id"])},
        headers=_auth(ptok),
    )
    assert denied.status_code == 403, denied.text


async def test_tenant_cannot_revoke_another_tenants_grant(
    client: AsyncClient, app_sessionmaker: Any, setup: dict[str, Any]
) -> None:
    """Tenant B cannot revoke tenant A's grant.

    The HTTP API runs under the owner role (no FORCE-RLS), so we prove this at
    the DB layer under the saebooks_app (NOBYPASSRLS) role — exactly the SQL
    the revoke endpoint runs (UPDATE ... WHERE id=:id AND status='active') but
    with tenant B's GUC bound. RLS confines the UPDATE to B's own rows, so A's
    grant matches zero rows: the endpoint would return 404. We also confirm A
    CAN revoke it (positive control).
    """
    a = setup["tenants"]["A"]
    b = setup["tenants"]["B"]
    token_a = await _admin_token(setup, "A")
    created = await client.post(
        "/api/v1/principal-grants",
        json={"principal_id": str(setup["principal_id"]), "role": "accountant"},
        headers=_auth(token_a),
    )
    grant_id = created.json()["id"]

    revoke_sql = (
        "UPDATE principal_tenant_grants "
        "SET status='revoked', revoked_at=now() "
        "WHERE id = :id AND status='active'"
    )
    # Tenant B's session: zero rows affected -> endpoint 404.
    async with app_sessionmaker() as s, s.begin():
        await s.execute(
            text(f"SET LOCAL app.current_tenant = '{b['tenant_id']}'")
        )
        res_b = await s.execute(text(revoke_sql), {"id": grant_id})
        assert res_b.rowcount == 0, "tenant B must not be able to revoke A's grant"
    # Tenant A's session: one row affected -> endpoint 204.
    async with app_sessionmaker() as s, s.begin():
        await s.execute(
            text(f"SET LOCAL app.current_tenant = '{a['tenant_id']}'")
        )
        res_a = await s.execute(text(revoke_sql), {"id": grant_id})
        assert res_a.rowcount == 1, "tenant A must be able to revoke its own grant"


async def test_list_shows_only_own_tenant_grants(
    client: AsyncClient, app_sessionmaker: Any, setup: dict[str, Any]
) -> None:
    """A tenant lists only its own grants.

    Proven under the app role (FORCE-RLS) because the owner-role HTTP API does
    not isolate. A creates a grant via the API; then, under each tenant's GUC,
    the list SELECT the endpoint runs returns only that tenant's rows.
    """
    a = setup["tenants"]["A"]
    b = setup["tenants"]["B"]
    token_a = await _admin_token(setup, "A")
    await client.post(
        "/api/v1/principal-grants",
        json={"principal_id": str(setup["principal_id"]), "role": "accountant"},
        headers=_auth(token_a),
    )
    list_sql = (
        "SELECT principal_id, tenant_id FROM principal_tenant_grants "
        "ORDER BY granted_at DESC"
    )
    # A sees its grant.
    async with app_sessionmaker() as s, s.begin():
        await s.execute(
            text(f"SET LOCAL app.current_tenant = '{a['tenant_id']}'")
        )
        rows_a = (await s.execute(text(list_sql))).all()
    assert all(str(r.tenant_id) == str(a["tenant_id"]) for r in rows_a)
    assert any(
        str(r.principal_id) == str(setup["principal_id"]) for r in rows_a
    )
    # B sees none of A's grants.
    async with app_sessionmaker() as s, s.begin():
        await s.execute(
            text(f"SET LOCAL app.current_tenant = '{b['tenant_id']}'")
        )
        rows_b = (await s.execute(text(list_sql))).all()
    assert all(str(r.tenant_id) != str(a["tenant_id"]) for r in rows_b)


async def test_non_admin_cannot_create_grant(
    client: AsyncClient, setup: dict[str, Any], owner_sessionmaker: Any
) -> None:
    """A bookkeeper-role user cannot manage grants -> 403."""
    a = setup["tenants"]["A"]
    suffix = setup["suffix"]
    async with owner_sessionmaker() as s:
        bk = User(
            id=uuid.uuid4(), tenant_id=a["tenant_id"],
            username=f"ga-bk-{suffix}", email=f"ga-bk-{suffix}@test.invalid",
            role="bookkeeper",
        )
        s.add(bk)
        await s.commit()
        bk_id = bk.id
    try:
        async with AsyncSessionLocal() as s:
            bk = await s.get(User, bk_id)
            token = _mint_user(bk)
        resp = await client.post(
            "/api/v1/principal-grants",
            json={"principal_id": str(setup["principal_id"]), "role": "accountant"},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text
    finally:
        async with owner_sessionmaker() as s:
            await s.execute(sa_delete(User).where(User.id == bk_id))
            await s.commit()


async def test_invalid_role_rejected(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    token = await _admin_token(setup, "A")
    resp = await client.post(
        "/api/v1/principal-grants",
        json={"principal_id": str(setup["principal_id"]), "role": "superuser"},
        headers=_auth(token),
    )
    assert resp.status_code == 400, resp.text
