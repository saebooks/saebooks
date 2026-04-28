"""Tests for MOTR-4: tracking_vehicle_id on bill lines.

Verifies that:
* tracking_vehicle_id is accepted on bill line create and returned in response
* tracking_vehicle_id=None is valid (field is optional)
* The field is persisted and round-trips correctly
"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact


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
async def bill_ids() -> dict[str, str]:
    """Return expense account id and contact id from the live test DB."""
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert expense is not None
    assert contact is not None
    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


def _payload(ids: dict[str, str], vehicle_id: str | None = None) -> dict:
    line: dict = {
        "account_id": ids["expense_account_id"],
        "description": "Floorplan interest — test vehicle",
        "quantity": 1,
        "unit_price": "150.00",
    }
    if vehicle_id is not None:
        line["tracking_vehicle_id"] = vehicle_id
    return {
        "contact_id": ids["contact_id"],
        "issue_date": date(2026, 5, 1).isoformat(),
        "due_date": date(2026, 5, 31).isoformat(),
        "lines": [line],
    }


async def test_bill_line_tracking_vehicle_id_round_trips(
    api_client: AsyncClient, bill_ids: dict[str, str]
) -> None:
    """A bill created with tracking_vehicle_id returns it in the response."""
    vin = "1HGBH41JXMN109186"
    r = await api_client.post("/api/v1/bills", json=_payload(bill_ids, vin))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["lines"][0]["tracking_vehicle_id"] == vin


async def test_bill_line_tracking_vehicle_id_none_ok(
    api_client: AsyncClient, bill_ids: dict[str, str]
) -> None:
    """A bill created without tracking_vehicle_id returns null in the response."""
    r = await api_client.post("/api/v1/bills", json=_payload(bill_ids))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["lines"][0]["tracking_vehicle_id"] is None


async def test_bill_line_tracking_vehicle_id_max_length(
    api_client: AsyncClient, bill_ids: dict[str, str]
) -> None:
    """tracking_vehicle_id longer than 64 chars is rejected as 422."""
    too_long = "X" * 65
    r = await api_client.post("/api/v1/bills", json=_payload(bill_ids, too_long))
    assert r.status_code == 422
