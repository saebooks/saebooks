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
from saebooks.models.bill import Bill, BillLine
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine
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
        assert c is not None
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


@pytest.mark.asyncio
async def test_invoice_form_retention_pct_persists(api_client: AsyncClient) -> None:
    """Form POST under /invoices must persist line_<i>_retention_pct."""
    form = {
        "contact_id": await _customer_id(),
        "issue_date": "2026-04-29",
        "due_date": "2026-05-29",
        "line_0_description": "Form retention test",
        "line_0_account_id": await _income_acct_id(),
        "line_0_tax_code_id": "",
        "line_0_quantity": "1",
        "line_0_unit_price": "200.00",
        "line_0_discount_pct": "0",
        "line_0_retention_pct": "7",
        "line_0_project_id": "",
    }
    r = await api_client.post(
        "/invoices", data=form, follow_redirects=False
    )
    assert r.status_code in (302, 303), r.text

    async with AsyncSessionLocal() as session:
        inv = (
            await session.execute(
                select(Invoice)
                .where(Invoice.archived_at.is_(None))
                .order_by(Invoice.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        ln = (
            await session.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
            )
        ).scalar_one()
        assert ln.retention_pct == Decimal("7")


@pytest.mark.asyncio
async def test_bill_form_retention_pct_persists(api_client: AsyncClient) -> None:
    """Form POST under /bills must persist line_<i>_retention_pct."""
    form = {
        "contact_id": await _supplier_id(),
        "issue_date": "2026-04-29",
        "due_date": "2026-05-29",
        "supplier_reference": "P0-3-form",
        "line_0_description": "Form retention test",
        "line_0_account_id": await _expense_acct_id(),
        "line_0_tax_code_id": "",
        "line_0_quantity": "1",
        "line_0_unit_price": "100.00",
        "line_0_discount_pct": "0",
        "line_0_retention_pct": "3",
        "line_0_project_id": "",
    }
    r = await api_client.post(
        "/bills", data=form, follow_redirects=False
    )
    assert r.status_code in (302, 303), r.text

    async with AsyncSessionLocal() as session:
        bill = (
            await session.execute(
                select(Bill)
                .where(Bill.archived_at.is_(None))
                .order_by(Bill.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        ln = (
            await session.execute(
                select(BillLine).where(BillLine.bill_id == bill.id)
            )
        ).scalar_one()
        assert ln.retention_pct == Decimal("3")
