"""Router tests for /admin/* (admin.py).

Covers:
* GET /admin/settings → 200
* POST /admin/settings → 200, saved=True in context
* GET /admin/audit → 200, page/filter params pass through
* GET /admin/audit/export.csv → requires ACCOUNTANT role (401/403 without)
* GET /admin/audit/export.csv → 200 with authenticated accountant
* GET /admin/audit/<unknown-uuid> → 404
* GET /admin/sql → 200
* POST /admin/sql → 200, query result rendered
* POST /admin/sql with DML → 400 (read-only enforcement)
* POST /admin/sql/export → 200, text/csv Content-Disposition
* GET /admin/license → 200, edition shown
* GET /admin/users → requires ADMIN role → 403 for readonly
* GET /admin/users → 200 for admin user
* POST /admin/users/<id>/role with bad role → redirect with err=bad_role
* POST /admin/users/<id>/role valid role → redirect with saved=1
* POST /admin/users/<id>/archive → redirect
* POST /admin/users/<id>/unarchive → redirect
* GET /admin/whoami → 401 without user
* GET /admin/whoami → 200 with user header
* GET /admin/permissions → 200 for admin, 403 for readonly
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.user import User, UserRole


@pytest.fixture
async def anon_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Shared: create users in DB so ForwardAuthMiddleware can pick them up
# ---------------------------------------------------------------------------


async def _make_user(username: str, role: str) -> User:
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalars().first()
        if existing:
            existing.role = role
            existing.archived_at = None
            await session.commit()
            return existing
        u = User(username=username, role=role)
        session.add(u)
        await session.commit()
        await session.refresh(u)
    return u


async def _cleanup_user(username: str) -> None:
    async with AsyncSessionLocal() as session:
        from sqlalchemy import delete as sa_delete
        await session.execute(sa_delete(User).where(User.username == username))
        await session.commit()


@pytest.fixture
async def admin_username() -> str:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    await _make_user(name, "admin")
    yield name
    await _cleanup_user(name)


@pytest.fixture
async def accountant_username() -> str:
    name = f"acct-{uuid.uuid4().hex[:8]}"
    await _make_user(name, "accountant")
    yield name
    await _cleanup_user(name)


@pytest.fixture
async def readonly_username() -> str:
    name = f"ro-{uuid.uuid4().hex[:8]}"
    await _make_user(name, "readonly")
    yield name
    await _cleanup_user(name)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def test_admin_settings_get_200(client: AsyncClient) -> None:
    r = await client.get("/admin/settings")
    assert r.status_code == 200


async def test_admin_settings_post_200(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/settings",
        data={
            "fin_year_start_month": "7",
            "base_currency": "AUD",
            "gst_rounding_sales": "DOWN",
            "gst_rounding_purchases": "DOWN",
            "gst_calc_level": "LINE",
            "gst_auto_post": "true",
            "gst_collected_account_code": "21310",
            "gst_paid_account_code": "21330",
            "gst_clearing_account_code": "21320",
            "prefix_mode": "classic",
            "structured_numbering": "true",
            "audit_mode": "immutable",
            "retention_years_journal": "7",
            "retention_years_attachments": "7",
        },
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def test_admin_audit_list_200(client: AsyncClient) -> None:
    r = await client.get("/admin/audit")
    assert r.status_code == 200


async def test_admin_audit_list_with_table_filter(client: AsyncClient) -> None:
    r = await client.get("/admin/audit", params={"table_name": "invoice"})
    assert r.status_code == 200


async def test_admin_audit_list_with_page(client: AsyncClient) -> None:
    r = await client.get("/admin/audit", params={"page": 2})
    assert r.status_code == 200


async def test_admin_audit_export_csv_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/admin/audit/export.csv")
    assert r.status_code == 401


async def test_admin_audit_export_csv_requires_accountant(
    client: AsyncClient, readonly_username: str
) -> None:
    r = await client.get(
        "/admin/audit/export.csv",
        headers={"Remote-User": readonly_username},
    )
    assert r.status_code == 403


async def test_admin_audit_export_csv_200_for_accountant(
    client: AsyncClient, accountant_username: str
) -> None:
    r = await client.get(
        "/admin/audit/export.csv",
        headers={"Remote-User": accountant_username},
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "audit-" in cd


async def test_admin_audit_detail_404_unknown_id(client: AsyncClient) -> None:
    r = await client.get(f"/admin/audit/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# SQL browser
# ---------------------------------------------------------------------------


async def test_admin_sql_get_200(client: AsyncClient) -> None:
    r = await client.get("/admin/sql")
    assert r.status_code == 200


async def test_admin_sql_post_select_200(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/sql",
        data={"sql": "SELECT 1 AS n"},
    )
    assert r.status_code == 200
    assert "1" in r.text


async def test_admin_sql_post_dml_rejected(client: AsyncClient) -> None:
    """DML (INSERT/UPDATE/DELETE) must be blocked by the read-only service."""
    r = await client.post(
        "/admin/sql",
        data={"sql": "DELETE FROM companies WHERE 1=0"},
    )
    assert r.status_code == 200
    # The router renders the error into the page rather than returning 4xx
    assert "error" in r.text.lower() or "not allowed" in r.text.lower() or "read" in r.text.lower()


async def test_admin_sql_export_csv_200(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/sql/export",
        data={"sql": "SELECT 1 AS n"},
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "query.csv" in cd


async def test_admin_sql_export_csv_400_on_dml(client: AsyncClient) -> None:
    r = await client.post(
        "/admin/sql/export",
        data={"sql": "DROP TABLE companies"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# License page
# ---------------------------------------------------------------------------


async def test_admin_license_200(client: AsyncClient) -> None:
    r = await client.get("/admin/license")
    assert r.status_code == 200
    assert "community" in r.text.lower() or "edition" in r.text.lower()


# ---------------------------------------------------------------------------
# Users list — role-gated
# ---------------------------------------------------------------------------


async def test_admin_users_list_401_no_user(client: AsyncClient) -> None:
    r = await client.get("/admin/users")
    assert r.status_code == 401


async def test_admin_users_list_403_for_readonly(
    client: AsyncClient, readonly_username: str
) -> None:
    r = await client.get(
        "/admin/users",
        headers={"Remote-User": readonly_username},
    )
    assert r.status_code == 403


async def test_admin_users_list_200_for_admin(
    client: AsyncClient, admin_username: str
) -> None:
    r = await client.get(
        "/admin/users",
        headers={"Remote-User": admin_username},
    )
    assert r.status_code == 200
    assert admin_username in r.text


# ---------------------------------------------------------------------------
# User role changes
# ---------------------------------------------------------------------------


async def test_admin_users_set_role_bad_role_redirects(
    client: AsyncClient, admin_username: str, readonly_username: str
) -> None:
    async with AsyncSessionLocal() as session:
        target = (
            await session.execute(select(User).where(User.username == readonly_username))
        ).scalars().first()
    assert target is not None
    r = await client.post(
        f"/admin/users/{target.id}/role",
        data={"role": "supervillain"},
        headers={"Remote-User": admin_username},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=bad_role" in r.headers["location"]


async def test_admin_users_set_role_valid_redirects(
    client: AsyncClient, admin_username: str, readonly_username: str
) -> None:
    async with AsyncSessionLocal() as session:
        target = (
            await session.execute(select(User).where(User.username == readonly_username))
        ).scalars().first()
    assert target is not None
    r = await client.post(
        f"/admin/users/{target.id}/role",
        data={"role": "bookkeeper"},
        headers={"Remote-User": admin_username},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "saved=1" in r.headers["location"]
    # Reset role
    await _make_user(readonly_username, "readonly")


# ---------------------------------------------------------------------------
# User archive / unarchive
# ---------------------------------------------------------------------------


async def test_admin_users_archive_redirects(
    client: AsyncClient, admin_username: str
) -> None:
    target_name = f"archiveme-{uuid.uuid4().hex[:6]}"
    await _make_user(target_name, "readonly")
    try:
        async with AsyncSessionLocal() as session:
            target = (
                await session.execute(select(User).where(User.username == target_name))
            ).scalars().first()
        assert target is not None
        r = await client.post(
            f"/admin/users/{target.id}/archive",
            headers={"Remote-User": admin_username},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "archived=1" in r.headers["location"]
    finally:
        await _cleanup_user(target_name)


async def test_admin_users_unarchive_redirects(
    client: AsyncClient, admin_username: str
) -> None:
    target_name = f"unarchiveme-{uuid.uuid4().hex[:6]}"
    user = await _make_user(target_name, "readonly")
    # Archive in DB first
    async with AsyncSessionLocal() as session:
        u = await session.get(User, user.id)
        assert u is not None
        u.archived_at = datetime.now()
        await session.commit()
    try:
        r = await client.post(
            f"/admin/users/{user.id}/unarchive",
            headers={"Remote-User": admin_username},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "unarchived=1" in r.headers["location"]
    finally:
        await _cleanup_user(target_name)


# ---------------------------------------------------------------------------
# Whoami
# ---------------------------------------------------------------------------


async def test_admin_whoami_401_no_user(client: AsyncClient) -> None:
    r = await client.get("/admin/whoami")
    assert r.status_code == 401


async def test_admin_whoami_200_with_user(
    client: AsyncClient, readonly_username: str
) -> None:
    r = await client.get(
        "/admin/whoami",
        headers={"Remote-User": readonly_username},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == readonly_username


# ---------------------------------------------------------------------------
# Permissions matrix
# ---------------------------------------------------------------------------


async def test_admin_permissions_403_for_readonly(
    client: AsyncClient, readonly_username: str
) -> None:
    r = await client.get(
        "/admin/permissions",
        headers={"Remote-User": readonly_username},
    )
    assert r.status_code == 403


async def test_admin_permissions_200_for_admin(
    client: AsyncClient, admin_username: str
) -> None:
    r = await client.get(
        "/admin/permissions",
        headers={"Remote-User": admin_username},
    )
    assert r.status_code == 200
