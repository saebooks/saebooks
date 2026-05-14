"""FX revaluation report tests — /api/v1/reports/fx_revaluation.

Covers:
* test_fx_revaluation_empty — no foreign-currency documents → empty items list
* test_fx_revaluation_foreign_invoice — USD invoice appears in report
* test_fx_revaluation_foreign_bill — USD bill appears in report
* test_fx_revaluation_base_currency_excluded — AUD invoice not in report
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact
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
async def fx_deps() -> dict[str, str]:
    """Return income account_id, expense account_id, and contact_id."""
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()
    assert income is not None, "Test DB has no INCOME account"
    assert expense is not None, "Test DB has no EXPENSE account"
    assert contact is not None, "Test DB has no contact"
    return {
        "income_account_id": str(income.id),
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoice_payload(deps: dict[str, str], currency: str = "AUD") -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-15",
        "due_date": "2026-05-15",
        "currency": currency,
        "lines": [
            {
                "description": "FX test invoice line",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "1000.00",
                "discount_pct": "0",
            }
        ],
    }


def _bill_payload(deps: dict[str, str], currency: str = "AUD") -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-15",
        "due_date": "2026-05-15",
        "currency": currency,
        "lines": [
            {
                "description": "FX test bill line",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": "500.00",
                "discount_pct": "0",
            }
        ],
    }


async def _create_and_post_invoice(
    client: AsyncClient, deps: dict[str, str], currency: str = "AUD"
) -> dict:
    """Create a DRAFT invoice with given currency, then POST it."""
    r = await client.post("/api/v1/invoices", json=_invoice_payload(deps, currency))
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/invoices/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _create_and_post_bill(
    client: AsyncClient, deps: dict[str, str], currency: str = "AUD"
) -> dict:
    """Create a DRAFT bill with given currency, then POST it."""
    r = await client.post("/api/v1/bills", json=_bill_payload(deps, currency))
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/bills/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fx_revaluation_empty(api_client: AsyncClient) -> None:
    """Report with a far-future as_of_date and no foreign docs returns empty items."""
    r = await api_client.get(
        "/api/v1/reports/fx_revaluation",
        params={"as_of_date": "1990-01-01", "base_currency": "AUD"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of_date"] == "1990-01-01"
    assert body["base_currency"] == "AUD"
    assert isinstance(body["items"], list)
    assert body["total_items"] == len(body["items"])
    # May have items from other tests but none should be before 1990
    assert all(item["entity_type"] in ("INVOICE", "BILL") for item in body["items"])


async def test_fx_revaluation_foreign_invoice_appears(
    api_client: AsyncClient, fx_deps: dict[str, str]
) -> None:
    """A POSTED USD invoice appears in the FX revaluation report."""
    inv = await _create_and_post_invoice(api_client, fx_deps, currency="USD")
    inv_id = inv["id"]

    r = await api_client.get(
        "/api/v1/reports/fx_revaluation",
        params={"as_of_date": "2026-12-31", "base_currency": "AUD"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    item_ids = [item["entity_id"] for item in body["items"]]
    assert inv_id in item_ids, f"Invoice {inv_id} not in FX report items"

    # Find the specific item and verify shape
    item = next(i for i in body["items"] if i["entity_id"] == inv_id)
    assert item["entity_type"] == "INVOICE"
    assert item["currency"] == "USD"
    assert item["original_amount"] == 1000.0
    assert item["amount_paid"] == 0.0
    assert item["outstanding_foreign"] == 1000.0
    assert item["outstanding_base"] is None
    assert "FX rate not available" in item["note"]
    assert body["note"] == "Live FX rates not configured. Amounts shown in original currency."


async def test_fx_revaluation_foreign_bill_appears(
    api_client: AsyncClient, fx_deps: dict[str, str]
) -> None:
    """A POSTED USD bill appears in the FX revaluation report."""
    bill = await _create_and_post_bill(api_client, fx_deps, currency="USD")
    bill_id = bill["id"]

    r = await api_client.get(
        "/api/v1/reports/fx_revaluation",
        params={"as_of_date": "2026-12-31", "base_currency": "AUD"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    item_ids = [item["entity_id"] for item in body["items"]]
    assert bill_id in item_ids, f"Bill {bill_id} not in FX report items"

    item = next(i for i in body["items"] if i["entity_id"] == bill_id)
    assert item["entity_type"] == "BILL"
    assert item["currency"] == "USD"
    assert item["original_amount"] == 500.0
    assert item["outstanding_base"] is None


async def test_fx_revaluation_base_currency_excluded(
    api_client: AsyncClient, fx_deps: dict[str, str]
) -> None:
    """A POSTED AUD invoice must NOT appear in the FX revaluation report."""
    inv = await _create_and_post_invoice(api_client, fx_deps, currency="AUD")
    inv_id = inv["id"]

    r = await api_client.get(
        "/api/v1/reports/fx_revaluation",
        params={"as_of_date": "2026-12-31", "base_currency": "AUD"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    item_ids = [item["entity_id"] for item in body["items"]]
    assert inv_id not in item_ids, (
        f"AUD invoice {inv_id} should be excluded from FX report"
    )
