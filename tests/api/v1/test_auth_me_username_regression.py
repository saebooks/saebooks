"""Regression test for /auth/me returning the 'username' field (P0-2 hotfix).

audit-trail reference: 10-deploy-and-validation-2026-04-26.md
Commits:              e63a329 (saebooks) — add username to /auth/me response

Why this file exists
--------------------
The field-mismatch bug in the admin-gate regression chain:
/api/v1/auth/me returned ``{id, email, name, role, tenant_id}`` but NOT
``username``.  saebooks-web's allowlist check did
``profile.get("username")`` which always fell back to ``""`` and never
matched the ``SAE_STAFF_USERNAMES`` value — so even when the bearer
call succeeded, the web layer still denied access.

Fix (e63a329) adds ``username`` to the ``UserProfile`` Pydantic model
and to the GET /auth/me response handler.  The existing test in
test_auth_login.py::test_me_with_valid_token_200 was updated to assert
``body["username"] == user.username``.

This file provides an explicit, named regression test for the same
behaviour so the connection to the audit trail is visible.

DB availability: tests skip cleanly when Postgres is unavailable.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import text

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-for-auth-me-regression")

from saebooks.db import AsyncSessionLocal, engine as _owner_engine  # noqa: E402
from saebooks.main import app  # noqa: E402
from saebooks.models.user import User  # noqa: E402
from saebooks.services.jwt_tokens import _reset_secret_cache, hash_password  # noqa: E402


# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.asyncio


async def _db_available() -> bool:
    try:
        async with _owner_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEST_EMAIL = "auth_me_regression@test.invalid"
_TEST_USERNAME = "auth_me_regression_user"
_TEST_PASSWORD = "hunter2-regression"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def me_user() -> AsyncIterator[User]:
    """Create a test user with a known username and password."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")

    _reset_secret_cache()

    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.email == _TEST_EMAIL))
        await session.flush()

        u = User(
            id=uuid.uuid4(),
            tenant_id=_DEFAULT_TENANT,
            username=_TEST_USERNAME,
            email=_TEST_EMAIL,
            role="viewer",
            password_hash=hash_password(_TEST_PASSWORD),
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)

    yield u

    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.email == _TEST_EMAIL))
        await session.commit()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_auth_me_returns_username_field(
    client: AsyncClient, me_user: User
) -> None:
    """GET /api/v1/auth/me must include 'username' in the response body.

    This is the field-mismatch regression from e63a329:
    saebooks-web's admin gate check used profile.get("username") which
    always returned "" before the field was added.  The presence of this
    field in /auth/me is the single change that lets the web layer
    correctly match SAE_STAFF_USERNAMES.
    """
    login_r = await client.post(
        "/api/v1/auth/login",
        json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD},
    )
    assert login_r.status_code == 200, (
        f"Login failed (test setup): {login_r.status_code} {login_r.text}"
    )
    token = login_r.json()["access_token"]

    me_r = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_r.status_code == 200, (
        f"/auth/me returned {me_r.status_code}: {me_r.text}"
    )
    body = me_r.json()

    # PRIMARY REGRESSION ASSERTION: username must be present.
    assert "username" in body, (
        "REGRESSION: 'username' field missing from /api/v1/auth/me response "
        "(audit-trail #10, commit e63a329 — field-mismatch admin gate bug)"
    )
    assert body["username"] == _TEST_USERNAME, (
        f"Expected username={_TEST_USERNAME!r}, got {body.get('username')!r}"
    )


async def test_auth_me_username_is_string(
    client: AsyncClient, me_user: User
) -> None:
    """The 'username' field must be a non-empty string, not None or ''."""
    login_r = await client.post(
        "/api/v1/auth/login",
        json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD},
    )
    assert login_r.status_code == 200
    token = login_r.json()["access_token"]

    me_r = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_r.status_code == 200
    body = me_r.json()

    username_val = body.get("username")
    assert isinstance(username_val, str) and username_val, (
        f"Expected non-empty string for 'username', got {username_val!r}"
    )


async def test_auth_me_other_fields_still_present(
    client: AsyncClient, me_user: User
) -> None:
    """Adding 'username' must not remove existing fields from /auth/me.

    Guards against a regression where the schema change accidentally
    drops id, email, role, or tenant_id.
    """
    login_r = await client.post(
        "/api/v1/auth/login",
        json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD},
    )
    assert login_r.status_code == 200
    token = login_r.json()["access_token"]

    me_r = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_r.status_code == 200
    body = me_r.json()

    for required_field in ("id", "username", "email", "role", "tenant_id"):
        assert required_field in body, (
            f"Field {required_field!r} missing from /auth/me response. "
            "The e63a329 schema change must not have removed existing fields."
        )

    assert body["id"] == str(me_user.id)
    assert body["email"] == _TEST_EMAIL
    assert body["role"] == "viewer"
    assert body["tenant_id"] == str(_DEFAULT_TENANT)


async def test_auth_me_without_token_401(client: AsyncClient) -> None:
    """No token → 401. (Countercheck so we know the test is meaningful.)"""
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
