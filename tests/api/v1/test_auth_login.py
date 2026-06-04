"""B/43 contract tests for POST /api/v1/auth/login, /refresh, GET /me.

Tests
-----
* Login success → 200 + access_token present
* Login wrong password → 401 (same detail as unknown email)
* Login unknown email → 401
* Login archived user → 403
* Token is a valid JWT with correct sub/exp claims
* /auth/me with valid token → 200 + correct user fields
* /auth/me without token → 401
* /auth/refresh with valid token → 200 + new token with fresh exp
* /auth/refresh with expired token → 401
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.user import User
from saebooks.services.jwt_tokens import (
    _b64url_decode,
    _reset_secret_cache,
    create_access_token,
    hash_password,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_jwt_secret() -> None:
    """Ensure a stable per-test secret key (avoids cross-test contamination)."""
    import os
    os.environ["SAEBOOKS_SECRET_KEY"] = "test-secret-key-for-b43-tests"
    _reset_secret_cache()
    yield
    _reset_secret_cache()


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_user(
    *,
    email: str | None = None,
    password: str | None = None,
    archived: bool = False,
) -> User:
    """Insert a test user directly into the DB and return the ORM row.

    If a specific email is supplied, any pre-existing row with that
    email is deleted first so repeated test runs on a persistent DB
    don't accumulate duplicate users.
    """
    username = f"testuser_{uuid.uuid4().hex[:8]}"
    email = email or f"{username}@example.com"
    pw_hash = hash_password(password) if password else None

    # Clean up any leftover row from a prior run with this email.
    from sqlalchemy import delete as sa_delete

    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.email == email))
        await session.commit()

    async with AsyncSessionLocal() as session:
        user = User(
            id=uuid.uuid4(),
            tenant_id=_DEFAULT_TENANT,
            username=username,
            email=email,
            role="viewer",
            password_hash=pw_hash,
            version=1,
        )
        session.add(user)
        await session.flush()
        if archived:
            from datetime import UTC, datetime

            user.archived_at = datetime.now(UTC)
        await session.commit()
        # Return a detached copy with all attributes loaded.
        uid = user.id

    async with AsyncSessionLocal() as session:
        return await session.get(User, uid)  # type: ignore[return-value]


def _decode_payload(token: str) -> dict:
    """Decode the JWT payload (no verification) for assertion purposes."""
    parts = token.split(".")
    assert len(parts) == 3, "Token must have 3 parts"
    return json.loads(_b64url_decode(parts[1]))


# ---------------------------------------------------------------------------
# POST /auth/login — success
# ---------------------------------------------------------------------------


async def test_login_success_200(client: AsyncClient) -> None:
    await _make_user(email="login_ok@test.com", password="hunter2")
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "login_ok@test.com", "password": "hunter2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 8 * 3600


# ---------------------------------------------------------------------------
# POST /auth/login — wrong password → 401
# ---------------------------------------------------------------------------


async def test_login_wrong_password_401(client: AsyncClient) -> None:
    await _make_user(email="login_wp@test.com", password="correct")
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "login_wp@test.com", "password": "wrong"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid credentials"


# ---------------------------------------------------------------------------
# POST /auth/login — unknown email → 401, same message
# ---------------------------------------------------------------------------


async def test_login_unknown_email_401(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@nowhere.invalid", "password": "anything"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid credentials"


# ---------------------------------------------------------------------------
# POST /auth/login — archived user → 403
# ---------------------------------------------------------------------------


async def test_login_archived_user_403(client: AsyncClient) -> None:
    await _make_user(email="login_arch@test.com", password="pw", archived=True)
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "login_arch@test.com", "password": "pw"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Token JWT structure — correct sub and exp claims
# ---------------------------------------------------------------------------


async def test_login_token_has_correct_claims(client: AsyncClient) -> None:
    user = await _make_user(email="login_claims@test.com", password="s3cr3t")
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "login_claims@test.com", "password": "s3cr3t"},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    payload = _decode_payload(token)

    assert payload["sub"] == str(user.id)
    assert payload["role"] == "viewer"
    assert payload["tenant_id"] == str(_DEFAULT_TENANT)
    # exp should be ~8 hours from now (allow ±60s for test execution time)
    expected_exp = int(time.time()) + 8 * 3600
    assert abs(payload["exp"] - expected_exp) < 60


# ---------------------------------------------------------------------------
# GET /auth/me — with valid token → 200 + correct profile
# ---------------------------------------------------------------------------


async def test_me_with_valid_token_200(client: AsyncClient) -> None:
    user = await _make_user(email="me_ok@test.com", password="pw")
    login_r = await client.post(
        "/api/v1/auth/login",
        json={"email": "me_ok@test.com", "password": "pw"},
    )
    assert login_r.status_code == 200
    token = login_r.json()["access_token"]

    r = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(user.id)
    assert body["email"] == "me_ok@test.com"
    assert body["role"] == "viewer"
    assert body["tenant_id"] == str(_DEFAULT_TENANT)
    # P0 regression — must include username so saebooks-web can match the
    # SAE_STAFF_USERNAMES allowlist (Taylor Riverside Round 1, Probe C).
    assert body["username"] == user.username


# ---------------------------------------------------------------------------
# GET /auth/me — without token → 401
# ---------------------------------------------------------------------------


async def test_me_without_token_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/refresh — with valid token → 200 + new token
# ---------------------------------------------------------------------------


async def test_refresh_with_valid_token_200(client: AsyncClient) -> None:
    user = await _make_user(email="refresh_ok@test.com", password="pw")
    login_r = await client.post(
        "/api/v1/auth/login",
        json={"email": "refresh_ok@test.com", "password": "pw"},
    )
    assert login_r.status_code == 200
    token = login_r.json()["access_token"]

    # Small sleep-free approach: grab original exp, then refresh.
    original_payload = _decode_payload(token)
    original_exp = original_payload["exp"]

    r = await client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    new_token = r.json()["access_token"]
    new_payload = _decode_payload(new_token)

    # New token has a fresh exp >= original (may be equal if issued in
    # the same second, but must not be earlier).
    assert new_payload["exp"] >= original_exp
    assert new_payload["sub"] == str(user.id)


# ---------------------------------------------------------------------------
# POST /auth/refresh — with expired token → 401
# ---------------------------------------------------------------------------


async def test_refresh_with_expired_token_401(client: AsyncClient) -> None:
    user = await _make_user(email="refresh_exp@test.com", password="pw")
    # Craft a token that expired in the past.
    expired_token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(_DEFAULT_TENANT), "role": "viewer"},
        expires_in_seconds=-1,
    )
    r = await client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/refresh — body form (refresh_token field)
# ---------------------------------------------------------------------------


async def test_refresh_via_body_field(client: AsyncClient) -> None:
    await _make_user(email="refresh_body@test.com", password="pw")
    login_r = await client.post(
        "/api/v1/auth/login",
        json={"email": "refresh_body@test.com", "password": "pw"},
    )
    assert login_r.status_code == 200
    token = login_r.json()["access_token"]

    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token},
    )
    assert r.status_code == 200, r.text
    assert "access_token" in r.json()
