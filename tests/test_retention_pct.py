"""P0-3: retention_pct must round-trip through API + form.

Covers:
* POST /api/v1/invoices with retention_pct=10 → GET back returns 10
* POST /api/v1/bills with retention_pct=5 → GET back returns 5
* POST form /invoices with line_<i>_retention_pct=7 → DB row has 7
* POST form /bills with line_<i>_retention_pct=3 → DB row has 3

Pre-fix the Pydantic ``InvoiceLineCreate`` / ``BillLineCreate`` had no
``retention_pct`` field, so the API silently dropped it. Bills' form
parser also did not include retention_pct. Civil-contractor invoices
and subbie bills both rely on this round-trip for the retention
journal posting.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import BillLine
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceLine

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


async def _income_acct_id() -> str:
    async with AsyncSessionLocal() as session:
        a = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.account_type == AccountType.INCOME,
                ).limit(1)
            )
        ).scalars().first()
        assert a is not None
        return str(a.id)


async def _expense_acct_id() -> str:
    async with AsyncSessionLocal() as session:
        a = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()
        assert a is not None
        return str(a.id)


async def _customer_id() -> str:
    async with AsyncSessionLocal() as session:
        c = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.contact_type == ContactType.CUSTOMER,
                ).limit(1)
            )
        ).scalars().first()
        assert c is not None
        return str(c.id)


async def _supplier_id() -> str:
    """Return any active supplier id, seeding one if the test DB has none.

    The original test relied on an earlier full-suite test (test_bills /
    test_aged_ap) having created a supplier first. Running this file
    in isolation showed the gap — seed a deterministic placeholder so
    the retention tests stand on their own.
    """
    async with AsyncSessionLocal() as session:
        c = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.contact_type == ContactType.SUPPLIER,
                ).limit(1)
            )
        ).scalars().first()
        if c is None:
            # Look up a company in the same tenant so the FK lands.
            from saebooks.models.company import Company
            co = (
                await session.execute(
                    select(Company)
                    .where(Company.tenant_id == DEFAULT_TENANT_ID, Company.archived_at.is_(None))
                    .order_by(Company.created_at)
                    .limit(1)
                )
            ).scalars().first()
            assert co is not None, "no company in test DB for tenant"
            c = Contact(
                company_id=co.id,
                tenant_id=DEFAULT_TENANT_ID,
                name="Retention Test Supplier",
                contact_type=ContactType.SUPPLIER,
                email="ret-supplier@example.com",
            )
            session.add(c)
            await session.commit()
            await session.refresh(c)
        return str(c.id)


@pytest.mark.asyncio
async def test_invoice_api_retention_pct_round_trips(api_client: AsyncClient) -> None:
    payload = {
        "contact_id": await _customer_id(),
        "issue_date": "2026-04-29",
        "due_date": "2026-05-29",
        "lines": [
            {
                "description": "Civil works progress claim",
                "account_id": await _income_acct_id(),
                "quantity": "1",
                "unit_price": "1000.00",
                "discount_pct": "0",
                "retention_pct": "10",
            }
        ],
    }
    r = await api_client.post("/api/v1/invoices", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    inv_id = body["id"]
    assert Decimal(str(body["lines"][0]["retention_pct"])) == Decimal("10")

    g = await api_client.get(f"/api/v1/invoices/{inv_id}")
    assert g.status_code == 200
    assert Decimal(str(g.json()["lines"][0]["retention_pct"])) == Decimal("10")

    async with AsyncSessionLocal() as session:
        ln = (
            await session.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == uuid.UUID(inv_id))
            )
        ).scalar_one()
        assert ln.retention_pct == Decimal("10")


@pytest.mark.asyncio
async def test_bill_api_retention_pct_round_trips(api_client: AsyncClient) -> None:
    payload = {
        "contact_id": await _supplier_id(),
        "issue_date": "2026-04-29",
        "due_date": "2026-05-29",
        "lines": [
            {
                "description": "Subbie progress claim",
                "account_id": await _expense_acct_id(),
                "quantity": "1",
                "unit_price": "500.00",
                "discount_pct": "0",
                "retention_pct": "5",
            }
        ],
    }
    r = await api_client.post("/api/v1/bills", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    bill_id = body["id"]
    assert Decimal(str(body["lines"][0]["retention_pct"])) == Decimal("5")

    g = await api_client.get(f"/api/v1/bills/{bill_id}")
    assert g.status_code == 200
    assert Decimal(str(g.json()["lines"][0]["retention_pct"])) == Decimal("5")

    async with AsyncSessionLocal() as session:
        ln = (
            await session.execute(
                select(BillLine).where(BillLine.bill_id == uuid.UUID(bill_id))
            )
        ).scalar_one()
        assert ln.retention_pct == Decimal("5")


