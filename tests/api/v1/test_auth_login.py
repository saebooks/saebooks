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

import base64
import json
import time
import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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
    from sqlalchemy import delete as sa_delete  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.email == email))
        await session.commit()

    async with AsyncSessionLocal() as session:
        user = User(
            id=uuid.uuid4(),
            tenant_id=_DEFAULT_TENANT,
            username=username,
            email=email,
            role="readonly",
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
    user = await _make_user(email="login_ok@test.com", password="hunter2")
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
    assert payload["role"] == "readonly"
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
    assert body["role"] == "readonly"
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
        {"sub": str(user.id), "tenant_id": str(_DEFAULT_TENANT), "role": "readonly"},
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
    user = await _make_user(email="refresh_body@test.com", password="pw")
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


# ---------------------------------------------------------------------------
# Regression: login email lookup must bypass RLS (post-squash defect)
# ---------------------------------------------------------------------------
#
# Background
# ----------
# In production app.saebooks.com.au, the api container connects via
# SAEBOOKS_APP_DATABASE_URL (the saebooks_app role: NOBYPASSRLS, FORCE
# RLS on users). Email lookup at login time is intrinsically pre-tenant
# — you cannot know which tenant a user belongs to before you've found
# them by email. The previous implementation opened the FORCE-RLS
# session for the email SELECT, the tenant_isolation policy returned
# zero rows, and login 401'd for every user.
#
# The fix makes the email lookup go through a narrow BYPASSRLS owner
# engine (mirroring saebooks/cli/seed_demo.py:_owner_session_factory).
# This test pins the runtime AsyncSessionLocal to a NOBYPASSRLS engine
# that matches prod's role topology, then exercises POST /auth/login
# end-to-end. Pre-fix, this test 401s. Post-fix, it 200s.


async def test_login_works_under_force_rls_app_role() -> None:
    """Regression: login must succeed when AsyncSessionLocal is bound to
    a FORCE-RLS role (saebooks_app shape) — the prod configuration.

    Skipped on SQLite or when the saebooks_app role is missing
    (migration 0056 not applied).
    """
    import os
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker as _async_sessionmaker,
        create_async_engine as _create_async_engine,
    )
    from sqlalchemy.pool import NullPool as _NullPool

    from saebooks.db import engine as _owner_engine
    from saebooks.api.v1 import login as login_module

    if not _owner_engine.url.get_backend_name().startswith("postgres"):
        pytest.skip("RLS regression is meaningless on SQLite.")

    # Make sure saebooks_app exists and we know its password.
    app_role_pw = "test-only-app-pw"
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            pytest.skip("saebooks_app role missing — migration 0056 not applied")
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{app_role_pw}'")
        )

    # Build a session factory bound to saebooks_app, same shape as prod.
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    app_url = (
        f"postgresql+asyncpg://saebooks_app:{app_role_pw}@db:5432/{db_name}"
    )
    app_engine = _create_async_engine(
        app_url, poolclass=_NullPool, future=True
    )
    app_session_factory = _async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )

    # Seed a known user via the owner engine (BYPASSRLS) — bypassing
    # _make_user so this test is self-contained and not coupled to the
    # role-enum drift in the rest of the suite.
    from sqlalchemy import delete as sa_delete

    seed_email = "rls_login@test.com"
    seed_pw = "rls-secret-1"
    Owner = _async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with Owner() as session:
        await session.execute(sa_delete(User).where(User.email == seed_email))
        session.add(
            User(
                id=uuid.uuid4(),
                tenant_id=_DEFAULT_TENANT,
                username=f"rls_{uuid.uuid4().hex[:8]}",
                email=seed_email,
                role="viewer",
                password_hash=hash_password(seed_pw),
                version=1,
            )
        )
        await session.commit()

    # Reset the cached owner-session factory the patched _user_by_email
    # uses so this test isn't influenced by an earlier import.
    if hasattr(login_module._owner_session_factory, "cache_clear"):
        login_module._owner_session_factory.cache_clear()

    # Pin AsyncSessionLocal in the login module to the FORCE-RLS factory.
    # The login route's email lookup must NOT use this factory after the
    # fix — it must build its own owner-bound session — so calling
    # /auth/login should still return 200.
    original_local = login_module.AsyncSessionLocal
    login_module.AsyncSessionLocal = app_session_factory  # type: ignore[assignment]
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.post(
                "/api/v1/auth/login",
                json={
                    "email": seed_email,
                    "password": seed_pw,
                },
            )
        assert r.status_code == 200, (
            f"login under FORCE-RLS app role failed: "
            f"{r.status_code} {r.text}"
        )
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
    finally:
        login_module.AsyncSessionLocal = original_local  # type: ignore[assignment]
        if hasattr(login_module._owner_session_factory, "cache_clear"):
            login_module._owner_session_factory.cache_clear()
        await app_engine.dispose()
