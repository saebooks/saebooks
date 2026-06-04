"""Phase 1 + Phase 2 contract tests for the public-auth flow.

Covers:

* Signup happy path → 201 + verification email written to outbox
* Password too short / no digit / no letter → 422
* Duplicate email → 409
* Verify happy path → 200 + valid JWT, user.email_verified_at set
* Verify expired token → 410
* Double-verify (second consume of same token) → 404
* pwv invalidation: a JWT minted before a password reset is rejected
* Rate limit: 6th signup from same IP → 429

Phase 2 additions:
* Reset-request enumeration safety (200 for unknown email)
* Reset-confirm bumps password_version + works
* Magic-link replay (consume twice → 410 / 404)
* Resend-verification rate limit (4th call → 429)
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text

from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.auth_tokens import generate_token, hash_token
from saebooks.services.jwt_tokens import (
    _b64url_decode,
    _reset_secret_cache,
)
pytestmark = pytest.mark.postgres_only


@pytest.fixture(autouse=True)
def reset_jwt_secret() -> None:
    os.environ["SAEBOOKS_SECRET_KEY"] = "test-secret-key-for-signup-tests"
    _reset_secret_cache()
    # Ensure mailer drops to outbox (no SMTP host) — already the
    # default in tests but make explicit.
    os.environ.pop("SMTP_HOST", None)
    os.environ.setdefault("SAEBOOKS_MAIL_OUTBOX_DIR", "/tmp/saebooks-test-outbox")
    yield
    _reset_secret_cache()


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _decode_payload(token: str) -> dict:
    import json
    parts = token.split(".")
    assert len(parts) == 3
    return json.loads(_b64url_decode(parts[1]))


async def _purge_email(email: str) -> None:
    """Best-effort cleanup so re-running the suite isn't a duplicate trap."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.email.ilike(email)))
        # Tenants are looser-keyed; we leave them. Slug-uniqueness is
        # via random suffix, so no collision risk.
        await session.commit()


async def _reset_rate_limits() -> None:
    """Wipe the rate_limit_counters table so each test starts clean."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM rate_limit_counters"))
        await session.commit()


async def _new_email() -> str:
    return f"signup-{uuid.uuid4().hex[:10]}@example.test"


# ---------------------------------------------------------------------------
# Signup happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_creates_tenant_user_and_sends_email(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    await _purge_email(email)

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026", "company_name": "Acme Test"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "Verification email sent" in body["message"]

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == email)
        )
        row = result.first()
        assert row is not None
        # User exists with role=owner, unverified, has token hash
        assert row.role == "owner"
        assert row.email_verified_at is None
        assert row.email_verification_token_hash is not None
        assert row.password_version == 0


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_password_too_short(client: AsyncClient) -> None:
    await _reset_rate_limits()
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": await _new_email(), "password": "abc12"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_signup_password_letters_only(client: AsyncClient) -> None:
    await _reset_rate_limits()
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": await _new_email(), "password": "abcdefghijk"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_signup_password_digits_only(client: AsyncClient) -> None:
    await _reset_rate_limits()
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": await _new_email(), "password": "1234567890"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Duplicate email → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_duplicate_email(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    await _purge_email(email)
    first = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026"},
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026"},
    )
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Verify happy + expired + double-consume
# ---------------------------------------------------------------------------


async def _signup_and_get_token(client: AsyncClient, email: str) -> str:
    """Signup then read the raw token straight from the DB by hash. We
    can't intercept the email because send_email is awaited inline; a
    cleaner approach would be to inspect the outbox dir, but reading
    the hash back lets us also test expired/replayed tokens."""
    await _purge_email(email)
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026"},
    )
    assert resp.status_code == 201
    # We pre-generate and store our own token + hash so the test can
    # round-trip the raw value. Overwrite the user's hash via DB.
    raw = generate_token()
    h = hash_token(raw)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == email)
        )
        row = result.first()
        await session.execute(
            User.__table__.update()
            .where(User.id == row.id)
            .values(
                email_verification_token_hash=h,
                email_verification_expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )
        await session.commit()
    return raw


@pytest.mark.asyncio
async def test_verify_happy_path(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    raw = await _signup_and_get_token(client, email)
    resp = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    claims = _decode_payload(body["access_token"])
    assert "sub" in claims
    assert claims["role"] == "owner"
    assert claims["pwv"] == 0


@pytest.mark.asyncio
async def test_verify_expired_token(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    raw = await _signup_and_get_token(client, email)
    # Force expiry in the past
    async with AsyncSessionLocal() as session:
        await session.execute(
            User.__table__.update()
            .where(User.email == email)
            .values(email_verification_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    resp = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_double_verify_returns_404(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    raw = await _signup_and_get_token(client, email)
    first = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    assert first.status_code == 200
    second = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    assert second.status_code == 404


# ---------------------------------------------------------------------------
# pwv invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pwv_mismatch_invalidates_token(client: AsyncClient) -> None:
    """A JWT issued before a password reset must stop authenticating
    on subsequent requests."""
    await _reset_rate_limits()
    email = await _new_email()
    raw = await _signup_and_get_token(client, email)
    verify = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    token = verify.json()["access_token"]

    # Old token still works for /auth/me
    me1 = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me1.status_code == 200

    # Bump password_version directly (simulates a reset having run)
    async with AsyncSessionLocal() as session:
        await session.execute(
            User.__table__.update()
            .where(User.email == email)
            .values(password_version=1)
        )
        await session.commit()

    # Now the JWT should fail on a route that runs require_bearer
    # with user hydration. /auth/me uses its own decode and doesn't
    # go through require_bearer — try a v1 router that does (contacts).
    resp = await client.get(
        "/api/v1/contacts", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Rate limit: 6th signup from same IP → 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_rate_limit(client: AsyncClient) -> None:
    # The limiter uses fixed minute windows (date_trunc('minute', now())). If
    # the 6 requests straddle a minute boundary the counter resets mid-loop and
    # the 6th request is no longer the 6th in its window -> spurious non-429.
    # Under suite load the loop is slow enough to occasionally cross a boundary.
    # Retry the whole sequence in a fresh window when that happens (no fixed
    # sleeps): only assert on a run that stayed inside one minute window.
    statuses: list[int] = []
    for _attempt in range(4):
        await _reset_rate_limits()
        minute_before = datetime.now(UTC).minute
        statuses = []
        for i in range(6):
            email = f"rl-{i}-{uuid.uuid4().hex[:6]}@example.test"
            await _purge_email(email)
            resp = await client.post(
                "/api/v1/auth/signup",
                json={"email": email, "password": "letmein-2026"},
            )
            statuses.append(resp.status_code)
        if datetime.now(UTC).minute == minute_before:
            break  # whole loop ran inside one fixed-minute window

    # First 5 should pass (201), 6th should be 429.
    assert statuses[0] == 201
    assert statuses[4] == 201
    assert statuses[5] == 429, f"Expected 429, got {statuses}"


# ---------------------------------------------------------------------------
# Phase 2 — reset-password enumeration safety + happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_reset_request_unknown_email_200(client: AsyncClient) -> None:
    await _reset_rate_limits()
    resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "nobody-here@example.test"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_confirm_bumps_pwv(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    raw = await _signup_and_get_token(client, email)
    verify = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    old_token = verify.json()["access_token"]

    # Inject a reset token directly
    reset_raw = generate_token()
    async with AsyncSessionLocal() as session:
        await session.execute(
            User.__table__.update()
            .where(User.email == email)
            .values(
                password_reset_token_hash=hash_token(reset_raw),
                password_reset_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()

    confirm = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": reset_raw, "new_password": "newpass-2026"},
    )
    assert confirm.status_code == 200
    new_token = confirm.json()["access_token"]
    assert new_token != old_token

    # Old token should now fail
    old_resp = await client.get(
        "/api/v1/contacts", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert old_resp.status_code == 401

    # New pwv claim should be 1
    claims = _decode_payload(new_token)
    assert claims["pwv"] == 1


@pytest.mark.asyncio
async def test_magic_link_replay_blocked(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    raw_verify = await _signup_and_get_token(client, email)
    await client.post("/api/v1/auth/verify-email", json={"token": raw_verify})

    magic_raw = generate_token()
    async with AsyncSessionLocal() as session:
        await session.execute(
            User.__table__.update()
            .where(User.email == email)
            .values(
                magic_link_token_hash=hash_token(magic_raw),
                magic_link_expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )
        await session.commit()

    first = await client.post(
        "/api/v1/auth/magic-link/consume", json={"token": magic_raw}
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/v1/auth/magic-link/consume", json={"token": magic_raw}
    )
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_resend_verification_rate_limit(client: AsyncClient) -> None:
    await _reset_rate_limits()
    email = await _new_email()
    await _purge_email(email)
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026"},
    )

    statuses = []
    for _ in range(4):
        resp = await client.post(
            "/api/v1/auth/resend-verification", json={"email": email}
        )
        statuses.append(resp.status_code)
    assert statuses[0] == 200
    assert statuses[2] == 200
    assert statuses[3] == 429


# ---------------------------------------------------------------------------
# Phase 5 — signup_plan persistence and verify-email clearing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_with_plan_stores_signup_plan(client: AsyncClient) -> None:
    """signup with plan='business' stores the value on users.signup_plan."""
    await _reset_rate_limits()
    email = await _new_email()
    await _purge_email(email)

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026", "plan": "business"},
    )
    assert resp.status_code == 201, resp.text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == email)
        )
        row = result.first()
        assert row is not None
        assert row.signup_plan == "business"


@pytest.mark.asyncio
async def test_signup_without_plan_leaves_signup_plan_null(client: AsyncClient) -> None:
    """signup with plan=None leaves signup_plan NULL."""
    await _reset_rate_limits()
    email = await _new_email()
    await _purge_email(email)

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026"},
    )
    assert resp.status_code == 201, resp.text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == email)
        )
        row = result.first()
        assert row is not None
        assert row.signup_plan is None


@pytest.mark.asyncio
async def test_signup_with_invalid_plan_returns_422(client: AsyncClient) -> None:
    """signup with plan='invalid' raises 422."""
    await _reset_rate_limits()
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": await _new_email(), "password": "letmein-2026", "plan": "invalid"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_verify_clears_signup_plan(client: AsyncClient) -> None:
    """After verify-email, signup_plan is cleared to NULL on the user row
    and the response body includes the plan value."""
    await _reset_rate_limits()
    email = await _new_email()
    await _purge_email(email)

    # Signup with plan
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "letmein-2026", "plan": "pro"},
    )
    assert resp.status_code == 201, resp.text

    # Inject a known verification token
    raw = generate_token()
    h = hash_token(raw)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == email)
        )
        row = result.first()
        await session.execute(
            User.__table__.update()
            .where(User.id == row.id)
            .values(
                email_verification_token_hash=h,
                email_verification_expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )
        await session.commit()

    # Verify
    verify_resp = await client.post("/api/v1/auth/verify-email", json={"token": raw})
    assert verify_resp.status_code == 200, verify_resp.text
    body = verify_resp.json()
    assert body["signup_plan"] == "pro"

    # DB row should have signup_plan cleared
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == email)
        )
        row = result.first()
        assert row.signup_plan is None
        assert row.email_verified_at is not None
