"""Regression tests for P0-2 admin gate (SAE_STAFF_USERNAMES allowlist).

audit-trail reference: 10-deploy-and-validation-2026-04-26.md
Commits:              0baf159 (saebooks) + 5db97ad + 32b8cd1 (saebooks-web)

Why this file exists
--------------------
The original audit trail (round 1) only tested that a bookkeeper was
blocked.  Taylor Riverside's round-2 probe discovered that 5db97ad also
locked out the SAE staff user ("richard") that it was meant to allow.
Three hotfixes later (32b8cd1, e63a329, 73493bb) the three-tier matrix
is correct:

  SAE staff (on SAE_STAFF_USERNAMES allowlist) → 200
  Bookkeeper (not on allowlist)                → 403
  Anonymous (no identity header)               → 401 or 303 → /login

This test file is the positive-control + negative-control harness so
that any future regression to any of these three states is caught.

The API-layer admin routes (/admin/audit, /admin/sql) use
``require_staff()`` from authz.py which reads ``request.state.user``
populated by ForwardAuthMiddleware.  The simplest way to trigger the
middleware path in tests is to send a ``Remote-User`` header (the
test-only trusted-header shape the middleware expects).

User setup
----------
We create two real User rows in the DB:
  - ``richard_stafftest`` on SAE_STAFF_USERNAMES so the gate passes.
  - ``chen_apex_stafftest`` off the allowlist so the gate denies.

We set ``SAE_STAFF_USERNAMES`` in the environment around each test so
the dep reads the correct value without polluting the global env.

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

from saebooks.db import AsyncSessionLocal, engine as _owner_engine  # noqa: E402
from saebooks.main import app  # noqa: E402
from saebooks.models.user import User  # noqa: E402


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
# Test usernames
# ---------------------------------------------------------------------------

_STAFF_USERNAME = "richard_stafftest"
_BOOKKEEPER_USERNAME = "chen_apex_stafftest"
_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Fixtures: create/destroy test users, set SAE_STAFF_USERNAMES.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def gate_users() -> AsyncIterator[None]:
    """Create both test users in the DB for this module."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")

    async with AsyncSessionLocal() as session:
        # Wipe any leftovers from a previous run.
        for uname in (_STAFF_USERNAME, _BOOKKEEPER_USERNAME):
            await session.execute(sa_delete(User).where(User.username == uname))
        await session.flush()

        session.add(
            User(
                id=uuid.uuid4(),
                tenant_id=_DEFAULT_TENANT,
                username=_STAFF_USERNAME,
                email=f"{_STAFF_USERNAME}@test.invalid",
                role="admin",
            )
        )
        session.add(
            User(
                id=uuid.uuid4(),
                tenant_id=_DEFAULT_TENANT,
                username=_BOOKKEEPER_USERNAME,
                email=f"{_BOOKKEEPER_USERNAME}@test.invalid",
                role="bookkeeper",
            )
        )
        await session.commit()

    yield

    async with AsyncSessionLocal() as session:
        for uname in (_STAFF_USERNAME, _BOOKKEEPER_USERNAME):
            await session.execute(sa_delete(User).where(User.username == uname))
        await session.commit()


@contextmanager
def _staff_env(usernames: list[str]):
    """Context manager: temporarily set SAE_STAFF_USERNAMES."""
    original = os.environ.get("SAE_STAFF_USERNAMES", "")
    os.environ["SAE_STAFF_USERNAMES"] = ",".join(usernames)
    try:
        yield
    finally:
        if original:
            os.environ["SAE_STAFF_USERNAMES"] = original
        else:
            os.environ.pop("SAE_STAFF_USERNAMES", None)


@pytest_asyncio.fixture
async def anon_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests — three-tier matrix
# ---------------------------------------------------------------------------


async def test_admin_gate_allows_sae_staff(gate_users: None) -> None:
    """SAE staff user on allowlist must reach /admin/audit → 200.

    This is the POSITIVE CONTROL: if this fails the allowlist gate is
    fail-closed for operators (the regression Taylor found).
    """
    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/audit",
                headers={"Remote-User": _STAFF_USERNAME},
            )
    assert r.status_code == 200, (
        f"SAE staff user should reach /admin/audit (200), got {r.status_code}: "
        f"{r.text[:200]}\n"
        "REGRESSION: admin gate is fail-closed for staff (audit-trail #10 P0-2)"
    )


async def test_admin_gate_allows_sae_staff_sql_tool(gate_users: None) -> None:
    """SAE staff user must reach /admin/sql → 200."""
    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/sql",
                headers={"Remote-User": _STAFF_USERNAME},
            )
    assert r.status_code == 200, (
        f"SAE staff user should reach /admin/sql (200), got {r.status_code}: "
        f"{r.text[:200]}"
    )


async def test_admin_gate_blocks_bookkeeper(gate_users: None) -> None:
    """Bookkeeper not on allowlist must be blocked at /admin/audit → 403."""
    with _staff_env([_STAFF_USERNAME]):  # bookkeeper is NOT in list
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/audit",
                headers={"Remote-User": _BOOKKEEPER_USERNAME},
            )
    assert r.status_code == 403, (
        f"Bookkeeper should be blocked at /admin/audit (403), got {r.status_code}"
    )


async def test_admin_gate_blocks_bookkeeper_sql_tool(gate_users: None) -> None:
    """Bookkeeper not on allowlist must be blocked at /admin/sql → 403."""
    with _staff_env([_STAFF_USERNAME]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/sql",
                headers={"Remote-User": _BOOKKEEPER_USERNAME},
            )
    assert r.status_code == 403, (
        f"Bookkeeper should be blocked at /admin/sql (403), got {r.status_code}"
    )


async def test_admin_gate_blocks_anonymous(anon_client: AsyncClient) -> None:
    """Anonymous request (no Remote-User header) must be blocked.

    The gate returns 401 (no user at all). saebooks-web then redirects
    to /login; the API itself returns 401.
    """
    with _staff_env([_STAFF_USERNAME]):
        r = await anon_client.get("/admin/audit")
    # Middleware leaves request.state.user=None → require_staff raises 401.
    assert r.status_code == 401, (
        f"Anonymous request should get 401 at /admin/audit, got {r.status_code}"
    )


async def test_admin_gate_anonymous_sql_tool(anon_client: AsyncClient) -> None:
    """Anonymous at /admin/sql → 401."""
    with _staff_env([_STAFF_USERNAME]):
        r = await anon_client.get("/admin/sql")
    assert r.status_code == 401, (
        f"Anonymous request should get 401 at /admin/sql, got {r.status_code}"
    )


async def test_admin_gate_empty_allowlist_blocks_everyone(gate_users: None) -> None:
    """When SAE_STAFF_USERNAMES is empty, EVERYONE is denied (fail-closed).

    This is the intended default for a freshly deployed instance with no
    operators configured.
    """
    with _staff_env([]):  # empty allowlist
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/admin/audit",
                headers={"Remote-User": _STAFF_USERNAME},
            )
    assert r.status_code == 403, (
        f"Empty allowlist should deny everyone (fail-closed), got {r.status_code}"
    )
