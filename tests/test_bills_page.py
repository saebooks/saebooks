"""Router smoke tests for ``/bills``.

Mirror of ``test_invoices_page.py`` without PDF/email/sent actions.

Covers:

* list page renders (with / without data)
* new-bill form renders (preview number + supplier + expense accounts)
* POST creates a DRAFT (redirect to detail page)
* DRAFT detail page shows Edit / Post / Discard
* Post transition renders POSTED state + Pay link
* Archive redirects to list
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "6-1000",
                )
            )
        ).scalar_one()

        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "GST",
                )
            )
        ).scalar_one()

        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Page Test Supplier",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Page Test Supplier",
                contact_type=ContactType.SUPPLIER,
                email="sup@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)

        return company.id, contact.id, expense.id, gst.id


@pytest.mark.asyncio
async def test_bills_list_renders(client: AsyncClient) -> None:
    r = await client.get("/bills")
    assert r.status_code == 200
    assert "Bills" in r.text


@pytest.mark.asyncio
async def test_bills_new_form_renders(client: AsyncClient) -> None:
    _cid, contact, _acct, _gst = await _ctx()
    r = await client.get("/bills/new")
    assert r.status_code == 200
    assert "New bill" in r.text
    assert "Page Test Supplier" in r.text
    assert str(contact) in r.text
    # Next number preview
    assert "BILL-" in r.text


@pytest.mark.asyncio
async def test_bills_post_creates_draft(client: AsyncClient) -> None:
    _cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    data = {
        "contact_id": str(contact),
        "supplier_reference": "SUP-999",
        "issue_date": today.isoformat(),
        "due_date": (today + timedelta(days=30)).isoformat(),
        "line_0_description": "Rent",
        "line_0_account_id": str(acct),
        "line_0_tax_code_id": str(gst),
        "line_0_quantity": "1",
        "line_0_unit_price": "1500",
        "line_0_discount_pct": "0",
        "notes": "",
    }
    r = await client.post("/bills", data=data, follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    assert r.headers["location"].startswith("/bills/")


@pytest.mark.asyncio
async def test_bills_detail_shows_draft_actions(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Office supplies",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("120"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    r = await client.get(f"/bills/{bill.id}")
    assert r.status_code == 200
    assert "DRAFT" in r.text
    assert "Edit" in r.text
    assert "Post" in r.text
    assert "Discard" in r.text


@pytest.mark.asyncio
async def test_bill_post_transitions_to_posted(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Line",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    r = await client.post(f"/bills/{bill.id}/post", follow_redirects=False)
    assert r.status_code in (302, 303)
    detail = await client.get(f"/bills/{bill.id}")
    assert detail.status_code == 200
    assert "POSTED" in detail.text
    # Posted bills expose a Pay link
    assert "/payments/new?direction=OUTGOING" in detail.text


@pytest.mark.asyncio
async def test_bill_archive_redirects(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Archive me",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("10"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    r = await client.post(f"/bills/{bill.id}/archive", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/bills"
