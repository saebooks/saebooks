"""Router smoke tests for ``/payments``.

Covers:

* list page renders (empty-state and with data)
* new-payment form renders with preview number, contacts, bank accounts
* POST /payments creates a DRAFT and redirects
* detail page shows status + draft actions (Post / Discard)
* Post transition flips state to POSTED and shows Void button
* Archive redirects back to list
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import payments as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, bank_account_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Page Test Payer",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Page Test Payer",
                contact_type=ContactType.CUSTOMER,
                email="payer@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        return company.id, contact.id, bank.id


@pytest.mark.asyncio
async def test_payments_list_renders(client: AsyncClient) -> None:
    r = await client.get("/payments")
    assert r.status_code == 200
    assert "Payments" in r.text


@pytest.mark.asyncio
async def test_payments_new_form_renders(client: AsyncClient) -> None:
    _cid, contact, _bank = await _ctx()
    r = await client.get("/payments/new")
    assert r.status_code == 200
    assert "New payment" in r.text
    assert "Page Test Payer" in r.text
    assert str(contact) in r.text
    assert "PAY-" in r.text


@pytest.mark.asyncio
async def test_payments_create_redirects(client: AsyncClient) -> None:
    _cid, contact, bank = await _ctx()
    data = {
        "contact_id": str(contact),
        "bank_account_id": str(bank),
        "payment_date": date(2026, 4, 20).isoformat(),
        "amount": "250.00",
        "direction": "INCOMING",
        "method": "eft",
        "reference": "TEST-REF",
        "notes": "",
    }
    r = await client.post("/payments", data=data, follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    assert r.headers["location"].startswith("/payments/")


@pytest.mark.asyncio
async def test_payments_detail_shows_draft_actions(client: AsyncClient) -> None:
    cid, contact, bank = await _ctx()
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 20),
            amount=Decimal("123.00"),
        )
    r = await client.get(f"/payments/{pay.id}")
    assert r.status_code == 200
    assert "DRAFT" in r.text
    assert "Post" in r.text
    assert "Discard" in r.text


@pytest.mark.asyncio
async def test_payment_post_transitions_to_posted(client: AsyncClient) -> None:
    cid, contact, bank = await _ctx()
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 20),
            amount=Decimal("75.00"),
        )
    r = await client.post(f"/payments/{pay.id}/post", follow_redirects=False)
    assert r.status_code in (302, 303)
    detail = await client.get(f"/payments/{pay.id}")
    assert detail.status_code == 200
    assert "POSTED" in detail.text
    # POSTED state exposes Void, not Post.
    assert "Void" in detail.text


@pytest.mark.asyncio
async def test_payment_archive_redirects(client: AsyncClient) -> None:
    cid, contact, bank = await _ctx()
    async with AsyncSessionLocal() as session:
        pay = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 20),
            amount=Decimal("5.00"),
        )
    r = await client.post(f"/payments/{pay.id}/archive", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/payments"
