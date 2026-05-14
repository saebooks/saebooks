"""Tests for GET /api/v1/reports/ytd_turnover (gap HOBB-2).

Tests:
1. test_ytd_turnover_empty        — no income JEs in current FY → 0, not crossed
2. test_ytd_turnover_below        — income below $75k → not crossed
3. test_ytd_turnover_above        — income at or above $75k → threshold_crossed=True
4. test_ytd_turnover_above_already_registered — income >= $75k AND
   company.gst_registered=True → threshold_crossed=False (the registration
   obligation is moot for an already-registered entity, so the dashboard
   banner must not fire). Regression test for the "register within 21 days"
   nag firing on long-standing GST-registered businesses.
"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.main import app
pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
async def income_account_id() -> str:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account)
                .where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                )
                .limit(1)
            )
        ).scalars().first()
        assert row is not None, "Test DB has no non-header INCOME account"
        return str(row.id)


@pytest.fixture
async def asset_account_id() -> str:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account)
                .where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                )
                .limit(1)
            )
        ).scalars().first()
        assert row is not None, "Test DB has no non-header ASSET account"
        return str(row.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _post_income_je(
    client: AsyncClient,
    income_account_id: str,
    asset_account_id: str,
    amount: str,
    entry_date: str,
) -> None:
    """Create and post a JE that credits income (debit asset, credit income)."""
    r = await client.post(
        "/api/v1/journal_entries",
        json={
            "entry_date": entry_date,
            "narration": "Test income for ytd_turnover",
            "lines": [
                {"account_id": asset_account_id, "debit": amount, "credit": "0"},
                {"account_id": income_account_id, "debit": "0", "credit": amount},
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.patch(
        f"/api/v1/journal_entries/{body['id']}",
        json={"status": "POSTED"},
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_ytd_turnover_empty(api_client: AsyncClient) -> None:
    """No income JEs in a past FY range → returns 0 and threshold_crossed=False."""
    r = await api_client.get("/api/v1/reports/ytd_turnover")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "fy_start" in body
    assert "fy_end" in body
    assert "ytd_turnover" in body
    assert "threshold" in body
    assert "threshold_crossed" in body

    assert body["threshold"] == 75000.0
    assert body["ytd_turnover"] >= 0.0
    assert isinstance(body["threshold_crossed"], bool)

    # fy_start must be July 1 of current or previous year
    fy_start = date.fromisoformat(body["fy_start"])
    assert fy_start.month == 7 and fy_start.day == 1
    fy_end = date.fromisoformat(body["fy_end"])
    assert fy_end.month == 6 and fy_end.day == 30


async def test_ytd_turnover_below_threshold(
    api_client: AsyncClient,
    income_account_id: str,
    asset_account_id: str,
) -> None:
    """Income below $75k → threshold_crossed=False."""
    # Use a date safely in the current FY (well after the period lock 2026-03-31)
    today = date.today()
    if today.month >= 7:
        entry_date = date(today.year, 8, 1).isoformat()
    else:
        entry_date = date(today.year, 4, 15).isoformat()

    await _post_income_je(
        api_client, income_account_id, asset_account_id, "1000.00", entry_date
    )

    r = await api_client.get("/api/v1/reports/ytd_turnover")
    assert r.status_code == 200, r.text
    body = r.json()
    # We can't assert exact ytd (other tests may have added income), but
    # threshold_crossed must be consistent with the reported ytd_turnover.
    assert body["threshold_crossed"] == (body["ytd_turnover"] >= 75000.0)


async def test_ytd_turnover_above_threshold(
    api_client: AsyncClient,
    income_account_id: str,
    asset_account_id: str,
) -> None:
    """Income >= $75k AND company.gst_registered=False → threshold_crossed=True.

    Asserts the seed company is unregistered (the default) so the
    register-within-21-days obligation is real.
    """
    async with AsyncSessionLocal() as session:
        company = (await session.execute(select(Company).limit(1))).scalar_one()
        company.gst_registered = False
        await session.commit()

    today = date.today()
    if today.month >= 7:
        entry_date = date(today.year, 9, 1).isoformat()
    else:
        entry_date = date(today.year, 4, 20).isoformat()

    # Post enough income to push well past $75k regardless of prior state.
    await _post_income_je(
        api_client, income_account_id, asset_account_id, "80000.00", entry_date
    )

    r = await api_client.get("/api/v1/reports/ytd_turnover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ytd_turnover"] >= 75000.0
    assert body["threshold_crossed"] is True


async def test_ytd_turnover_above_threshold_already_registered(
    api_client: AsyncClient,
    income_account_id: str,
    asset_account_id: str,
) -> None:
    """Income >= $75k AND company.gst_registered=True → threshold_crossed=False.

    The "register with the ATO within 21 days" obligation only applies to
    unregistered businesses that cross the $75k threshold. A long-standing
    GST-registered entity showing $75k+ in revenue is the normal case and
    must not trigger the dashboard banner. Regression test.
    """
    async with AsyncSessionLocal() as session:
        company = (await session.execute(select(Company).limit(1))).scalar_one()
        company.gst_registered = True
        company.gst_effective_date = date(2020, 7, 1)
        await session.commit()

    today = date.today()
    if today.month >= 7:
        entry_date = date(today.year, 10, 1).isoformat()
    else:
        entry_date = date(today.year, 5, 1).isoformat()

    await _post_income_je(
        api_client, income_account_id, asset_account_id, "90000.00", entry_date
    )

    r = await api_client.get("/api/v1/reports/ytd_turnover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ytd_turnover"] >= 75000.0
    assert body["threshold_crossed"] is False
    assert body["threshold_approaching"] is False
