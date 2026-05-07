"""Regression tests for /reports/* HTML routes and /api/v1/reports/* 401 regression.

audit-trail reference: 10-deploy-and-validation-2026-04-26.md
Commits:              ff00ca0 (per-router scoping cycle)

Why this file exists
--------------------
The per-router scoping refactor in ff00ca0 also repaired a 401 regression
on the HTML report pages (/reports/balance-sheet, /reports/profit-loss,
/reports/trial-balance).  Karen Walsh confirmed round-2: all three
pages returned 200 where round-1 had returned 401.

Two concerns this test covers:

1. HTML report routes (/reports/{balance-sheet,profit-loss,trial-balance})
   return 200 when a valid bearer token is supplied and 200 even without
   auth (the HTML routes have no require_user dep — they are public,
   gated only at the Caddy / forward-auth edge in production).

2. JSON API routes (/api/v1/reports/profit_loss, /api/v1/reports/balance_sheet,
   /api/v1/reports/trial_balance) return 200 with a valid bearer token.
   Before ff00ca0 these returned 401 because require_bearer was missing
   from the router dependencies.

DB availability: tests skip cleanly when Postgres is unavailable.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-for-reports-regression")

from saebooks.api.v1.auth import current_token  # noqa: E402
from saebooks.db import engine as _owner_engine  # noqa: E402
from saebooks.main import app  # noqa: E402


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
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def authed_client() -> AsyncIterator[AsyncClient]:
    """Client with the static dev bearer token for JSON API calls."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def anon_client() -> AsyncIterator[AsyncClient]:
    """Client without any auth headers for HTML route tests."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# HTML report routes — no auth dep, must return 200.
#
# These were 401 before ff00ca0 because a middleware-level change
# accidentally put require_bearer in front of the HTML router.
# ---------------------------------------------------------------------------


async def test_reports_balance_sheet_html_200(anon_client: AsyncClient) -> None:
    """GET /reports/balance-sheet → 200 (no auth required for HTML route).

    Karen Walsh's round-2 probe: was 401 in round 1, should be 200 now.
    """
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await anon_client.get("/reports/balance-sheet")
    assert r.status_code == 200, (
        f"REGRESSION: /reports/balance-sheet returned {r.status_code} (expected 200). "
        "Was 401 before ff00ca0 fix."
    )
    assert "text/html" in r.headers.get("content-type", ""), (
        f"Expected HTML response, got content-type: {r.headers.get('content-type')}"
    )


async def test_reports_profit_loss_html_200(anon_client: AsyncClient) -> None:
    """GET /reports/profit-loss → 200."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await anon_client.get("/reports/profit-loss")
    assert r.status_code == 200, (
        f"REGRESSION: /reports/profit-loss returned {r.status_code} (expected 200). "
        "Was 401 before ff00ca0 fix."
    )


async def test_reports_trial_balance_html_200(anon_client: AsyncClient) -> None:
    """GET /reports/trial-balance → 200."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await anon_client.get("/reports/trial-balance")
    assert r.status_code == 200, (
        f"REGRESSION: /reports/trial-balance returned {r.status_code} (expected 200). "
        "Was 401 before ff00ca0 fix."
    )


# ---------------------------------------------------------------------------
# JSON API report routes — require bearer, must return 200 with token.
#
# Before ff00ca0, these returned 401 because the per-router scoping refactor
# missed adding ``require_bearer`` to the api/v1/reports router.
# ---------------------------------------------------------------------------


async def test_api_reports_profit_loss_200(authed_client: AsyncClient) -> None:
    """GET /api/v1/reports/profit_loss with bearer → 200.

    This was the regression: the v1 reports router was missing require_bearer
    after the scoping refactor, causing all report endpoints to 401.
    """
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await authed_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "1999-01-01", "to_date": "1999-12-31"},
    )
    assert r.status_code == 200, (
        f"REGRESSION: /api/v1/reports/profit_loss returned {r.status_code} with "
        "valid bearer (expected 200 — was 401 before ff00ca0 fix)"
    )


async def test_api_reports_balance_sheet_200(authed_client: AsyncClient) -> None:
    """GET /api/v1/reports/balance_sheet with bearer → 200."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await authed_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "1999-12-31"},
    )
    assert r.status_code == 200, (
        f"REGRESSION: /api/v1/reports/balance_sheet returned {r.status_code} with "
        "valid bearer (expected 200 — was 401 before ff00ca0 fix)"
    )


async def test_api_reports_trial_balance_200(authed_client: AsyncClient) -> None:
    """GET /api/v1/reports/trial_balance with bearer → 200."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await authed_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "1999-12-31"},
    )
    assert r.status_code == 200, (
        f"REGRESSION: /api/v1/reports/trial_balance returned {r.status_code} with "
        "valid bearer (expected 200 — was 401 before ff00ca0 fix)"
    )


async def test_api_reports_require_bearer_401_without_token(
    anon_client: AsyncClient,
) -> None:
    """GET /api/v1/reports/profit_loss without bearer → 401.

    The JSON API route must stay gated — this test ensures we don't
    accidentally remove the require_bearer dep again.
    """
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    r = await anon_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "1999-01-01", "to_date": "1999-12-31"},
    )
    assert r.status_code == 401, (
        f"API reports without bearer should return 401, got {r.status_code}. "
        "This would mean require_bearer was removed from the router."
    )


async def test_reports_index_html_200(anon_client: AsyncClient) -> None:
    """GET /reports → 200 (index page added in 0eac5a4)."""
    r = await anon_client.get("/reports")
    # The index page doesn't need DB.
    assert r.status_code == 200, (
        f"/reports index page returned {r.status_code} (expected 200)"
    )
