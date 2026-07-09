"""Tests for launch-promo integration in the signup flow.

Covers:
- Signup with promo OFF → launch_promo_jwt is NULL on user row
- Signup with promo ON + license-server returns token → JWT stamped on user
- Signup with promo ON + license-server returns 410 (exhausted) → user
  created without JWT (Community tier — promo stamp absent)
- Signup with promo ON + license-server network error → user created,
  promo_jwt NULL (best-effort)
- GET /api/v1/license/promo-stats returns correct shape when flag off
"""
from __future__ import annotations

import os

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.user import User
from saebooks.services import launch_promo as _promo_mod
from saebooks.services.jwt_tokens import _reset_secret_cache

pytestmark = pytest.mark.postgres_only


@pytest.fixture(autouse=True)
def reset_jwt_secret():
    os.environ["SAEBOOKS_SECRET_KEY"] = "test-secret-key-promo-tests"
    _reset_secret_cache()
    os.environ.pop("SMTP_HOST", None)
    os.environ.setdefault("SAEBOOKS_MAIL_OUTBOX_DIR", "/tmp/saebooks-promo-test-outbox")
    yield
    _reset_secret_cache()


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def _purge_email(email: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.email.ilike(email)))
        await session.commit()


async def _reset_rate_limits() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM rate_limit_counters"))
        await session.commit()


async def _get_user_promo_jwt(email: str) -> str | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.email.ilike(email))
        )
        user = result.scalars().first()
        if user is None:
            return None
        return user.launch_promo_jwt


# ---------------------------------------------------------------------------
# promo OFF (default)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_signup_promo_off_no_jwt_stamped(client, monkeypatch):
    """When LAUNCH_PROMO_ENABLED=false, signup succeeds and promo_jwt is NULL."""
    from saebooks.config import Settings as _Cfg
    cfg = _Cfg(
        LAUNCH_PROMO_ENABLED="false",
        DATABASE_URL=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        ),
    )
    monkeypatch.setattr(_promo_mod, "_settings", cfg)

    email = "promo-off-test@example.com"
    await _purge_email(email)
    await _reset_rate_limits()

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "TestPass1234",
            "company_name": "Promo Off Co",
        },
    )
    assert resp.status_code == 201, resp.text

    jwt = await _get_user_promo_jwt(email)
    assert jwt is None, f"Expected no promo JWT but got: {jwt!r}"

    await _purge_email(email)


# ---------------------------------------------------------------------------
# promo ON — successful claim
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_signup_promo_on_stamps_jwt(client, monkeypatch):
    """Signup with promo enabled and license-server returning a token stamps it."""
    from saebooks.config import Settings as _Cfg
    cfg = _Cfg(
        LAUNCH_PROMO_ENABLED="true",
        LICENSE_SERVER_URL="http://license-test-mock",
        DATABASE_URL=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        ),
    )
    monkeypatch.setattr(_promo_mod, "_settings", cfg)

    respx.post(
        "http://license-test-mock/api/v1/license/issue-launch-promo"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "mock.pro.jwt",
                "edition": "pro",
                "license_id": "lic_PROMO001",
                "expires_at": "2027-05-08T00:00:00+00:00",
                "promo": "first-1000-launch",
                "promo_slot": 7,
                "idempotent": False,
            },
        )
    )

    email = "promo-on-claim@example.com"
    await _purge_email(email)
    await _reset_rate_limits()

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "TestPass1234",
            "company_name": "Promo On Co",
        },
    )
    assert resp.status_code == 201, resp.text

    jwt = await _get_user_promo_jwt(email)
    assert jwt == "mock.pro.jwt", f"Expected JWT stamped but got: {jwt!r}"

    await _purge_email(email)


# ---------------------------------------------------------------------------
# promo ON — exhausted (410)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_signup_promo_exhausted_succeeds_without_jwt(client, monkeypatch):
    """Signup succeeds even when promo counter is exhausted (410 from server)."""
    from saebooks.config import Settings as _Cfg
    cfg = _Cfg(
        LAUNCH_PROMO_ENABLED="true",
        LICENSE_SERVER_URL="http://license-test-mock",
        DATABASE_URL=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        ),
    )
    monkeypatch.setattr(_promo_mod, "_settings", cfg)

    respx.post(
        "http://license-test-mock/api/v1/license/issue-launch-promo"
    ).mock(
        return_value=httpx.Response(410, json={"error": "promo_exhausted"})
    )

    email = "promo-exhausted@example.com"
    await _purge_email(email)
    await _reset_rate_limits()

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "TestPass1234",
            "company_name": "Late Co",
        },
    )
    assert resp.status_code == 201, resp.text

    jwt = await _get_user_promo_jwt(email)
    assert jwt is None

    await _purge_email(email)


# ---------------------------------------------------------------------------
# promo ON — license-server unreachable
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_signup_promo_network_error_still_succeeds(client, monkeypatch):
    """Signup succeeds even when license-server is unreachable."""
    from saebooks.config import Settings as _Cfg
    cfg = _Cfg(
        LAUNCH_PROMO_ENABLED="true",
        LICENSE_SERVER_URL="http://license-test-mock",
        DATABASE_URL=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        ),
    )
    monkeypatch.setattr(_promo_mod, "_settings", cfg)

    respx.post(
        "http://license-test-mock/api/v1/license/issue-launch-promo"
    ).mock(side_effect=httpx.ConnectError("connection refused"))

    email = "promo-neterror@example.com"
    await _purge_email(email)
    await _reset_rate_limits()

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "TestPass1234",
            "company_name": "Down Server Co",
        },
    )
    assert resp.status_code == 201, resp.text

    jwt = await _get_user_promo_jwt(email)
    assert jwt is None

    await _purge_email(email)


# ---------------------------------------------------------------------------
# GET /api/v1/license/promo-stats — public endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_promo_stats_endpoint_flag_off(client, monkeypatch):
    """Public promo-stats endpoint returns disabled shape when flag is off."""
    from saebooks.config import Settings as _Cfg
    cfg = _Cfg(
        LAUNCH_PROMO_ENABLED="false",
        LAUNCH_PROMO_LIMIT="1000",
        DATABASE_URL=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        ),
    )
    monkeypatch.setattr(_promo_mod, "_settings", cfg)

    resp = await client.get("/api/v1/license/promo-stats")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enabled"] is False
    assert data["limit"] == 1000
    assert data["remaining"] == 1000
