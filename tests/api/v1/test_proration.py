"""HTTP-tier tests for /api/v1/proration/*.

Covers:
* Auth gate (401)
* POST /preview — generic prorate
* POST /first-period-preview
* POST /plan-change-preview
* POST /deferred-revenue/preview — empty result when nothing to recognise
* POST /deferred-revenue/recognize — empty path returns posted=false
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #


async def test_proration_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.post(
        "/api/v1/proration/preview",
        json={
            "full_period_amount": "100",
            "basis": "MONTHLY",
            "service_start": "2026-01-01",
            "service_end": "2026-01-15",
        },
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Generic preview (Prorate #3)
# --------------------------------------------------------------------------- #


async def test_proration_preview_partial_month(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/proration/preview",
        json={
            "full_period_amount": "3000",
            "basis": "MONTHLY",
            "service_start": "2026-01-01",
            "service_end": "2026-01-13",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days_used"] == 13
    assert body["days_in_full"] == 31
    assert float(body["prorated_amount"]) == 1258.06


async def test_proration_preview_invalid_basis_422(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/proration/preview",
        json={
            "full_period_amount": "100",
            "basis": "FORTNIGHTLY",  # not a valid ProrateBasis
            "service_start": "2026-01-01",
            "service_end": "2026-01-15",
        },
    )
    assert r.status_code == 422


async def test_proration_preview_end_before_start_422(
    api_client: AsyncClient,
) -> None:
    r = await api_client.post(
        "/api/v1/proration/preview",
        json={
            "full_period_amount": "100",
            "basis": "MONTHLY",
            "service_start": "2026-02-01",
            "service_end": "2026-01-31",
        },
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# First-period preview (Prorate #1)
# --------------------------------------------------------------------------- #


async def test_proration_first_period_preview(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/proration/first-period-preview",
        json={
            "full_period_amount": "99",
            "basis": "MONTHLY",
            "service_start": "2026-04-18",
            "service_end": "2026-04-30",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days_used"] == 13
    assert body["days_in_full"] == 30
    assert float(body["prorated_amount"]) == 42.90
    assert "Pro-rata 13 of 30 days" in body["line_description_suggestion"]


# --------------------------------------------------------------------------- #
# Plan-change preview (Prorate #2)
# --------------------------------------------------------------------------- #


async def test_proration_plan_change_preview(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/proration/plan-change-preview",
        json={
            "old_period_amount": "99",
            "new_period_amount": "149",
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "change_date": "2026-04-16",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days_used"] == 15
    assert body["days_remaining"] == 15
    assert float(body["credit"]) == 49.50
    assert float(body["charge"]) == 74.50
    assert float(body["net"]) == 25.00


async def test_proration_plan_change_outside_period_422(
    api_client: AsyncClient,
) -> None:
    r = await api_client.post(
        "/api/v1/proration/plan-change-preview",
        json={
            "old_period_amount": "99",
            "new_period_amount": "149",
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "change_date": "2026-05-01",
        },
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Deferred-revenue (Prorate #4)
# --------------------------------------------------------------------------- #


async def test_proration_deferred_revenue_preview_empty(
    api_client: AsyncClient,
) -> None:
    """Without any deferred lines posted in the seed DB, preview is empty."""
    r = await api_client.post(
        "/api/v1/proration/deferred-revenue/preview",
        json={"period_date": "2026-01-15"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period_first"] == "2026-01-01"
    assert body["lines"] == []
    assert float(body["total_recognized"]) == 0.0


async def test_proration_deferred_revenue_recognize_empty(
    api_client: AsyncClient,
) -> None:
    """Recognise on an empty period returns posted=false."""
    r = await api_client.post(
        "/api/v1/proration/deferred-revenue/recognize",
        json={"period_date": "2026-01-15"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["posted"] is False
    assert body["lines_recognized"] == 0
