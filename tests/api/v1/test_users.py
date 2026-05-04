"""Phase 1 contract tests for /api/v1/users and /api/v1/permissions.

Covers:
* Auth gate (401 without bearer)
* Admin gate (403 without X-Admin: true on admin-only endpoints)
* List — 200, admin-only, optional role filter
* Get — 200, 404
* Create — 201, change_log row, version=1, password not in response
* Update with correct If-Match — version bumped, change_log row
* Update with stale If-Match → 409 with current state
* Update without If-Match → 428
* Non-admin cannot change role → 403
* Delete (soft-archive) — 204, archived_at set, version bumped
* Delete with stale If-Match → 409
* Delete without If-Match → 428
* change_log rows: create + update + archive in order
* GET /api/v1/permissions — full catalogue list
* GET /api/v1/users/{id}/permissions — resolved permission set
* PUT /api/v1/users/{id}/permissions — replace overrides (admin only)
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.change_log import ChangeLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    """Authenticated bearer client — no admin header."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def admin_client() -> AsyncClient:
    """Authenticated bearer client with X-Admin: true."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", "X-Admin": "true"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _rand_username() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Auth gate — bearer required everywhere
# ---------------------------------------------------------------------------


async def test_users_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/users")
    assert r.status_code == 401


async def test_users_get_one_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get(f"/api/v1/users/{uuid.uuid4()}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Admin gate — list/create/delete require X-Admin: true
# ---------------------------------------------------------------------------


async def test_users_list_requires_admin(api_client: AsyncClient) -> None:
    """List endpoint should 403 when bearer is present but X-Admin is absent."""
    r = await api_client.get("/api/v1/users")
    assert r.status_code == 403


async def test_users_create_requires_admin(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/users",
        json={"username": _rand_username(), "role": "viewer"},
    )
    assert r.status_code == 403


async def test_users_delete_requires_admin(
    api_client: AsyncClient, admin_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]
    v = cr.json()["version"]

    # Non-admin delete should 403
    r = await api_client.delete(f"/api/v1/users/{uid}", headers={"If-Match": str(v)})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_users_list_200(admin_client: AsyncClient) -> None:
    r = await admin_client.get("/api/v1/users")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_users_list_filter_by_role(admin_client: AsyncClient) -> None:
    uname = _rand_username()
    await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "accountant"}
    )
    r = await admin_client.get("/api/v1/users", params={"role": "accountant"})
    assert r.status_code == 200
    body = r.json()
    for item in body["items"]:
        assert item["role"] == "accountant"
    usernames = [i["username"] for i in body["items"]]
    assert uname in usernames


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_users_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/users/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_users_get_200(admin_client: AsyncClient, api_client: AsyncClient) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r = await api_client.get(f"/api/v1/users/{uid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == uid
    assert body["username"] == uname
    # Password must never appear
    assert "password" not in body
    assert "password_hash" not in body


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_users_create_201(admin_client: AsyncClient) -> None:
    uname = _rand_username()
    r = await admin_client.post(
        "/api/v1/users",
        json={
            "username": uname,
            "display_name": "Test User",
            "email": "test@example.com",
            "role": "bookkeeper",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == uname
    assert body["role"] == "bookkeeper"
    assert body["version"] == 1
    assert body["archived_at"] is None
    # Password never exposed
    assert "password" not in body
    assert "password_hash" not in body


async def test_users_create_duplicate_username_409(admin_client: AsyncClient) -> None:
    uname = _rand_username()
    r1 = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert r1.status_code == 201
    r2 = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert r2.status_code == 409


async def test_users_create_invalid_role_422(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/api/v1/users",
        json={"username": _rand_username(), "role": "superuser"},
    )
    assert r.status_code == 422


async def test_users_create_change_log(admin_client: AsyncClient) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    uname = _rand_username()
    r = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert r.status_code == 201
    uid = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(uid),
                    ChangeLog.entity == "user",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1
    assert rows[0].payload["username"] == uname
    # Payload must not contain password
    assert "password" not in rows[0].payload
    assert "password_hash" not in rows[0].payload


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_users_update_bumps_version(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]
    v = cr.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/users/{uid}",
        json={"display_name": "Updated Name"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["display_name"] == "Updated Name"


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_users_update_requires_if_match(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r2 = await api_client.patch(f"/api/v1/users/{uid}", json={"display_name": "x"})
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_users_stale_if_match_returns_409(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/users/{uid}",
        json={"display_name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == uid
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Non-admin cannot change role
# ---------------------------------------------------------------------------


async def test_non_admin_cannot_change_role(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]
    v = cr.json()["version"]

    r = await api_client.patch(
        f"/api/v1/users/{uid}",
        json={"role": "admin"},
        headers={"If-Match": str(v)},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Soft-delete (archive)
# ---------------------------------------------------------------------------


async def test_users_soft_delete_204(admin_client: AsyncClient) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]
    v = cr.json()["version"]

    r2 = await admin_client.delete(
        f"/api/v1/users/{uid}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    # Archived user no longer appears in the active list
    r3 = await admin_client.get("/api/v1/users")
    ids = [u["id"] for u in r3.json()["items"]]
    assert uid not in ids


async def test_users_delete_stale_if_match_409(admin_client: AsyncClient) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r2 = await admin_client.delete(
        f"/api/v1/users/{uid}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_users_delete_requires_if_match(admin_client: AsyncClient) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r2 = await admin_client.delete(f"/api/v1/users/{uid}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log: create + update + archive sequence
# ---------------------------------------------------------------------------


async def test_users_change_log_on_writes(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    await api_client.patch(
        f"/api/v1/users/{uid}",
        json={"display_name": "After create"},
        headers={"If-Match": "1"},
    )
    await admin_client.delete(
        f"/api/v1/users/{uid}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(uid),
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
    assert rows[0].entity == "user"
    assert rows[0].payload["username"] == uname


# ---------------------------------------------------------------------------
# Permission catalogue — GET /api/v1/permissions
# ---------------------------------------------------------------------------


async def test_permissions_list_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/permissions")
    assert r.status_code == 401


async def test_permissions_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/permissions")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # Catalogue seeded by 0033_permissions — should be non-empty if migration ran
    if body:
        assert "code" in body[0]
        assert "description" in body[0]


# ---------------------------------------------------------------------------
# Per-user permissions
# ---------------------------------------------------------------------------


async def test_user_permissions_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/users/{uuid.uuid4()}/permissions")
    assert r.status_code == 404


async def test_user_permissions_get_200(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r = await api_client.get(f"/api/v1/users/{uid}/permissions")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # Each item has code + description + resolved
    for item in body:
        assert "code" in item
        assert "description" in item
        assert "resolved" in item
        assert isinstance(item["resolved"], bool)


async def test_user_permissions_put_requires_admin(
    admin_client: AsyncClient, api_client: AsyncClient
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r = await api_client.put(
        f"/api/v1/users/{uid}/permissions",
        json={"grants": [], "revokes": []},
    )
    assert r.status_code == 403


async def test_user_permissions_put_unknown_code_422(
    admin_client: AsyncClient,
) -> None:
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    r = await admin_client.put(
        f"/api/v1/users/{uid}/permissions",
        json={"grants": ["nonexistent.permission"], "revokes": []},
    )
    assert r.status_code == 422


async def test_user_permissions_put_overlap_422(
    admin_client: AsyncClient,
) -> None:
    """A code cannot be in both grants and revokes."""
    uname = _rand_username()
    cr = await admin_client.post(
        "/api/v1/users", json={"username": uname, "role": "viewer"}
    )
    assert cr.status_code == 201
    uid = cr.json()["id"]

    # Use an empty body first to see if we can do a no-op 204
    r_ok = await admin_client.put(
        f"/api/v1/users/{uid}/permissions",
        json={"grants": [], "revokes": []},
    )
    assert r_ok.status_code == 204

    # Overlap between grants and revokes
    # We need a real code — look up the catalogue first
    catalogue_r = await admin_client.get("/api/v1/permissions")
    if catalogue_r.status_code == 200 and catalogue_r.json():
        code = catalogue_r.json()[0]["code"]
        r = await admin_client.put(
            f"/api/v1/users/{uid}/permissions",
            json={"grants": [code], "revokes": [code]},
        )
        assert r.status_code == 422
