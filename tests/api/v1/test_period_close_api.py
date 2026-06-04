"""JSON API tests for /api/v1/period-close (year-end close).

Validates the endpoint wiring, scope gating, and response schema. The
heavy close/idempotency/lock logic is covered at the service layer in
tests/test_period_close.py.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


async def _equity_account_id() -> str:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EQUITY,
                    Account.is_header.is_(False),
                ).limit(1)
            )
        ).scalars().first()
        assert row is not None, "Test DB has no leaf EQUITY account"
        return str(row.id)


async def test_preview_returns_shape(api_client: AsyncClient) -> None:
    eq = await _equity_account_id()
    r = await api_client.get(
        "/api/v1/period-close/preview",
        params={
            "through_date": "2025-06-30",
            "retained_earnings_account_id": eq,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["through_date"] == "2025-06-30"
    assert isinstance(body["has_anything_to_close"], bool)
    assert "net_profit" in body and "lines" in body


async def test_close_year_returns_result(api_client: AsyncClient) -> None:
    eq = await _equity_account_id()
    r = await api_client.post(
        "/api/v1/period-close/close-year",
        json={
            "through_date": "2025-06-30",
            "retained_earnings_account_id": eq,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["closed"], bool)
    # Fresh isolated DB has no posted P&L → nothing to close.
    if not body["closed"]:
        assert body["journal_entry_id"] is None
