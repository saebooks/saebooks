"""Notes/reference-only PATCH on non-DRAFT expenses.

Mirrors tests/api/v1/test_invoice_notes_on_posted.py — non-financial
metadata may be corrected after posting; financial fields stay DRAFT-only.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType

pytestmark = pytest.mark.postgres_only


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
async def expense_deps() -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        asset = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.is_header.is_(False),
                    Account.account_type == AccountType.ASSET,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        exp = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.is_header.is_(False),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
    assert asset is not None, "Test DB has no ASSET account"
    assert exp is not None, "Test DB has no EXPENSE account"
    return {"payment_account_id": str(asset.id), "expense_account_id": str(exp.id)}


async def _posted_expense(client: AsyncClient, deps: dict[str, str]) -> dict:
    r = await client.post(
        "/api/v1/expenses",
        json={
            "payment_account_id": deps["payment_account_id"],
            "expense_date": "2026-06-01",
            "notes": "books-rebuild-2026",
            "lines": [
                {
                    "description": "Consumables",
                    "account_id": deps["expense_account_id"],
                    "quantity": "1",
                    "unit_price": "50.00",
                },
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r = await client.post(
        f"/api/v1/expenses/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["status"] == "POSTED"
    return posted


@pytest.mark.asyncio
async def test_notes_only_patch_on_posted_expense(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    exp = await _posted_expense(api_client, expense_deps)

    r = await api_client.patch(
        f"/api/v1/expenses/{exp['id']}",
        headers={"If-Match": str(exp["version"])},
        json={"notes": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notes"] == ""
    assert body["version"] == exp["version"] + 1
    assert body["status"] == "POSTED"
    assert body["total"] == exp["total"]


@pytest.mark.asyncio
async def test_reference_only_patch_on_posted_expense(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    exp = await _posted_expense(api_client, expense_deps)

    r = await api_client.patch(
        f"/api/v1/expenses/{exp['id']}",
        headers={"If-Match": str(exp["version"])},
        json={"reference": "RECEIPT-42"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["reference"] == "RECEIPT-42"


@pytest.mark.asyncio
async def test_lines_patch_on_posted_expense_still_rejected(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    exp = await _posted_expense(api_client, expense_deps)

    r = await api_client.patch(
        f"/api/v1/expenses/{exp['id']}",
        headers={"If-Match": str(exp["version"])},
        json={
            "notes": "sneaky",
            "lines": [
                {
                    "description": "Altered",
                    "account_id": expense_deps["expense_account_id"],
                    "quantity": "1",
                    "unit_price": "999.00",
                },
            ],
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_expense_date_patch_on_posted_still_rejected(
    api_client: AsyncClient, expense_deps: dict[str, str]
) -> None:
    exp = await _posted_expense(api_client, expense_deps)

    r = await api_client.patch(
        f"/api/v1/expenses/{exp['id']}",
        headers={"If-Match": str(exp["version"])},
        json={"expense_date": "2026-01-01"},
    )
    assert r.status_code == 422, r.text
