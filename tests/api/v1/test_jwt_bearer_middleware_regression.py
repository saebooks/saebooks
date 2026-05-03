"""Regression tests for JWT bearer support in ForwardAuthMiddleware (P0-2 hotfix).

audit-trail reference: 10-deploy-and-validation-2026-04-26.md
Commits:              73493bb (saebooks) — accept JWT bearer in
                      ForwardAuthMiddleware so admin HTML routes work via web.

Why this file exists
--------------------
The third compounding bug in the admin-gate regression:
ForwardAuthMiddleware only populated ``request.state.user`` from
the test-only Remote-User trusted-header path.
It never looked at the ``Authorization: Bearer <jwt>`` header that
saebooks-web sends on its internal proxy calls.  Result: after the web
layer cleared richard, the API still 401'd because ``request.state.user``
was None when the request arrived with only a JWT, no Remote-User header.

Fix (73493bb) adds a JWT bearer fallback branch: when no Remote-User header
is present AND the request path is not in OPEN_PATH_PREFIXES, the middleware
decodes the ``Authorization: Bearer <jwt>`` header and resolves the user.

This test exercises that branch directly: a JWT minted for a real user is
sent to an admin route WITHOUT any Remote-User header.  Before 73493bb this
would return 401; after 73493bb it should return the gated response
(200 for staff, 403 for non-staff).

DB availability: tests skip cleanly when Postgres is unavailable.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import text

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-for-jwt-bearer-middleware")

from saebooks.db import AsyncSessionLocal, engine as _owner_engine  # noqa: E402
from saebooks.main import app  # noqa: E402
from saebooks.models.user import User  # noqa: E402
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token  # noqa: E402


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

_STAFF_USERNAME = "richard_jwttest"
_READONLY_USERNAME = "readonly_jwttest"
_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def jwt_users() -> AsyncIterator[dict[str, User]]:
    """Create a staff user and a readonly user.  Return their ORM rows."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")

    _reset_secret_cache()

    async with AsyncSessionLocal() as session:
        for uname in (_STAFF_USERNAME, _READONLY_USERNAME):
            await session.execute(sa_delete(User).where(User.username == uname))
        await session.flush()

        staff = User(
            id=uuid.uuid4(),
            tenant_id=_DEFAULT_TENANT,
            username=_STAFF_USERNAME,
            email=f"{_STAFF_USERNAME}@test.invalid",
            role="admin",
        )
        readonly = User(
            id=uuid.uuid4(),
            tenant_id=_DEFAULT_TENANT,
            username=_READONLY_USERNAME,
            email=f"{_READONLY_USERNAME}@test.invalid",
            role="viewer",
        )
        session.add_all([staff, readonly])
        await session.commit()
        await session.refresh(staff)
        await session.refresh(readonly)

    yield {"staff": staff, "readonly": readonly}

    async with AsyncSessionLocal() as session:
        for uname in (_STAFF_USERNAME, _READONLY_USERNAME):
            await session.execute(sa_delete(User).where(User.username == uname))
        await session.commit()


@contextmanager
def _staff_env(usernames: list[str]):
    """Temporarily set SAE_STAFF_USERNAMES."""
    original = os.environ.get("SAE_STAFF_USERNAMES", "")
    os.environ["SAE_STAFF_USERNAMES"] = ",".join(usernames)
    try:
        yield
    finally:
        if original:
            os.environ["SAE_STAFF_USERNAMES"] = original
        else:
            os.environ.pop("SAE_STAFF_USERNAMES", None)


def _mint_for(user: User) -> str:
    """Mint a JWT for the given user row."""
    _reset_secret_cache()
    return create_access_token(
        {
            "sub": str(user.id),
            "role": user.role,
            "tenant_id": str(user.tenant_id),
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_forward_auth_middleware_accepts_jwt_bearer_for_staff(
    jwt_users: dict[str, User],
) -> None:
    """JWT bearer (no Remote-User header) reaches /admin/audit → 200 for staff.

    This is the exact regression Taylor discovered: saebooks-web sends
    ``Authorization: Bearer <jwt>`` on its internal proxy call to
    /admin/audit.  Before 73493bb, request.state.user was None so
    require_staff() raised 401.
    """
    staff_user = jwt_users["staff"]
    token = _mint_for(staff_user)

    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/audit",
                # JWT bearer only — NO Remote-User header.
                headers={"Authorization": f"Bearer {token}"},
            )

    assert r.status_code == 200, (
        f"Staff JWT bearer should reach /admin/audit (200), got {r.status_code}: "
        f"{r.text[:300]}\n"
        "REGRESSION: ForwardAuthMiddleware not populating request.state.user "
        "from JWT bearer (audit-trail #10, commit 73493bb)"
    )


async def test_forward_auth_middleware_accepts_jwt_bearer_for_sql_tool(
    jwt_users: dict[str, User],
) -> None:
    """JWT bearer reaches /admin/sql → 200 for staff."""
    staff_user = jwt_users["staff"]
    token = _mint_for(staff_user)

    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/sql",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert r.status_code == 200, (
        f"Staff JWT bearer should reach /admin/sql (200), got {r.status_code}: "
        f"{r.text[:200]}"
    )


async def test_forward_auth_middleware_jwt_bearer_non_staff_blocked(
    jwt_users: dict[str, User],
) -> None:
    """JWT bearer for non-staff user is allowed through the middleware
    (user is populated), but then blocked by require_staff() → 403.

    Verifies the middleware correctly populates request.state.user from
    the JWT, then the authz dep makes the gating decision.
    """
    readonly_user = jwt_users["readonly"]
    token = _mint_for(readonly_user)

    # readonly_jwttest is NOT on the staff allowlist.
    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/audit",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert r.status_code == 403, (
        f"Non-staff JWT bearer should be 403 at /admin/audit, got {r.status_code}"
    )


async def test_forward_auth_middleware_invalid_jwt_bearer_is_anonymous() -> None:
    """A malformed bearer token must NOT crash the middleware (serve anonymously).

    The middleware returns None for invalid tokens so the request is treated
    as anonymous.  require_staff() then returns 401.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(
            "/admin/audit",
            headers={"Authorization": "Bearer not.a.valid.token"},
        )

    # Middleware falls through to anonymous → require_staff raises 401.
    assert r.status_code == 401, (
        f"Invalid bearer should be treated as anonymous (401), got {r.status_code}"
    )


async def test_forward_auth_middleware_no_bearer_no_headers_is_anonymous() -> None:
    """No bearer and no Remote-User header → anonymous → 401 at admin route."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/admin/audit")

    assert r.status_code == 401, (
        f"No-auth request should get 401 at /admin/audit, got {r.status_code}"
    )


async def test_forward_auth_middleware_jwt_bearer_takes_precedence_over_expired(
    jwt_users: dict[str, User],
) -> None:
    """An expired JWT in the bearer header is treated as anonymous → 401.

    The middleware calls _user_from_jwt_bearer which calls
    decode_access_token; an expired token raises JWTError and the
    function returns None.
    """
    staff_user = jwt_users["staff"]
    _reset_secret_cache()
    expired_token = create_access_token(
        {
            "sub": str(staff_user.id),
            "role": staff_user.role,
            "tenant_id": str(staff_user.tenant_id),
        },
        expires_in_seconds=-1,
    )

    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/audit",
                headers={"Authorization": f"Bearer {expired_token}"},
            )

    # Expired → anonymous → 401.
    assert r.status_code == 401, (
        f"Expired JWT bearer should be treated as anonymous (401), "
        f"got {r.status_code}"
    )
