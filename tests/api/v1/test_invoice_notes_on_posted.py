"""Non-financial PATCH (notes/payment_terms/due_date) on non-DRAFT invoices.

Non-financial metadata may be corrected after posting (it never feeds
totals, GST or the posted JE — due_date only drives aging/display);
financial fields (lines, issue_date, settlement_date, contact) stay
DRAFT-only.

Tests:
* notes-only PATCH on POSTED → 200, notes updated, version bumped
* payment_terms-only PATCH on POSTED → 200
* due_date-only PATCH on POSTED → 200, due_date updated, version bumped
* lines PATCH on POSTED → 422 invoice_not_draft (unchanged behaviour)
* issue_date PATCH on POSTED → 422 invoice_not_draft
* due_date + lines mixed PATCH on POSTED → 422 invoice_not_draft
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
async def invoice_deps() -> dict[str, str]:
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
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
    assert income is not None, "Test DB has no INCOME account"
    assert contact is not None, "Test DB has no contact"
    return {"income_account_id": str(income.id), "contact_id": str(contact.id)}


async def _posted_invoice(client: AsyncClient, deps: dict[str, str]) -> dict:
    r = await client.post(
        "/api/v1/invoices",
        json={
            "contact_id": deps["contact_id"],
            "issue_date": "2026-06-01",
            "due_date": "2026-07-01",
            "notes": "QBO DocNumber 9999",
            "lines": [
                {
                    "description": "Widget",
                    "account_id": deps["income_account_id"],
                    "quantity": "1",
                    "unit_price": "100.00",
                    "discount_pct": "0",
                },
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r = await client.post(
        f"/api/v1/invoices/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["status"] == "POSTED"
    return posted


@pytest.mark.asyncio
async def test_notes_only_patch_on_posted(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    inv = await _posted_invoice(api_client, invoice_deps)

    r = await api_client.patch(
        f"/api/v1/invoices/{inv['id']}",
        headers={"If-Match": str(inv["version"])},
        json={"notes": "Paid 1 Jun 2026 (RCT-TEST)."},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notes"] == "Paid 1 Jun 2026 (RCT-TEST)."
    assert body["version"] == inv["version"] + 1
    assert body["status"] == "POSTED"
    # Financial identity untouched
    assert body["total"] == inv["total"]
    assert body["subtotal"] == inv["subtotal"]


@pytest.mark.asyncio
async def test_payment_terms_only_patch_on_posted(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    inv = await _posted_invoice(api_client, invoice_deps)

    r = await api_client.patch(
        f"/api/v1/invoices/{inv['id']}",
        headers={"If-Match": str(inv["version"])},
        json={"payment_terms": "14 days EOM"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payment_terms"] == "14 days EOM"


@pytest.mark.asyncio
async def test_due_date_only_patch_on_posted(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    inv = await _posted_invoice(api_client, invoice_deps)

    r = await api_client.patch(
        f"/api/v1/invoices/{inv['id']}",
        headers={"If-Match": str(inv["version"])},
        json={"due_date": "2026-08-15"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["due_date"] == "2026-08-15"
    assert body["version"] == inv["version"] + 1
    assert body["status"] == "POSTED"
    # Financial identity untouched
    assert body["total"] == inv["total"]
    assert body["subtotal"] == inv["subtotal"]
    assert body["issue_date"] == inv["issue_date"]


@pytest.mark.asyncio
async def test_due_date_with_lines_patch_on_posted_rejected(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    """Mixing an allowed field with a financial one must still 422."""
    inv = await _posted_invoice(api_client, invoice_deps)

    r = await api_client.patch(
        f"/api/v1/invoices/{inv['id']}",
        headers={"If-Match": str(inv["version"])},
        json={
            "due_date": "2026-08-15",
            "lines": [
                {
                    "description": "Altered",
                    "account_id": invoice_deps["income_account_id"],
                    "quantity": "1",
                    "unit_price": "999.00",
                    "discount_pct": "0",
                },
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert "invoice_not_draft" in r.text


@pytest.mark.asyncio
async def test_lines_patch_on_posted_still_rejected(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    inv = await _posted_invoice(api_client, invoice_deps)

    r = await api_client.patch(
        f"/api/v1/invoices/{inv['id']}",
        headers={"If-Match": str(inv["version"])},
        json={
            "notes": "sneaky",
            "lines": [
                {
                    "description": "Altered",
                    "account_id": invoice_deps["income_account_id"],
                    "quantity": "1",
                    "unit_price": "999.00",
                    "discount_pct": "0",
                },
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert "invoice_not_draft" in r.text


@pytest.mark.asyncio
async def test_issue_date_patch_on_posted_still_rejected(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    inv = await _posted_invoice(api_client, invoice_deps)

    r = await api_client.patch(
        f"/api/v1/invoices/{inv['id']}",
        headers={"If-Match": str(inv["version"])},
        json={"issue_date": "2026-01-01"},
    )
    assert r.status_code == 422, r.text
    assert "invoice_not_draft" in r.text
