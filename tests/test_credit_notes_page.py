"""Router smoke tests for ``/credit-notes``.

Covers:

* list page renders
* new form renders with preview number + contacts + income accounts
* POST /credit-notes creates a DRAFT and redirects
* DRAFT detail page shows Edit / Post / Discard
* Post transition flips state to POSTED and exposes Void
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
from saebooks.models.tax_code import TaxCode
from saebooks.services import credit_notes as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_account_id, gst_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None
        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "4-6000",
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
                    Contact.name == "Page Test Refund",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Page Test Refund",
                contact_type=ContactType.CUSTOMER,
                email="refund@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        return company.id, contact.id, income.id, gst.id


def _line(acct: uuid.UUID, gst: uuid.UUID, amt: Decimal) -> dict[str, object]:
    return {
        "description": "Return",
        "account_id": acct,
        "tax_code_id": gst,
        "quantity": Decimal("1"),
        "unit_price": amt,
        "discount_pct": Decimal("0"),
    }


@pytest.mark.asyncio
async def test_credit_notes_list_renders(client: AsyncClient) -> None:
    r = await client.get("/credit-notes")
    assert r.status_code == 200
    assert "Credit notes" in r.text


@pytest.mark.asyncio
async def test_credit_notes_new_form_renders(client: AsyncClient) -> None:
    _cid, contact, _acct, _gst = await _ctx()
    r = await client.get("/credit-notes/new")
    assert r.status_code == 200
    assert "New credit note" in r.text
    assert "Page Test Refund" in r.text
    assert str(contact) in r.text
    assert "CN-" in r.text


@pytest.mark.asyncio
async def test_credit_notes_create_redirects(client: AsyncClient) -> None:
    _cid, contact, acct, gst = await _ctx()
    data = {
        "contact_id": str(contact),
        "issue_date": date(2026, 4, 20).isoformat(),
        "reason": "Test return",
        "line_0_description": "Refund line",
        "line_0_account_id": str(acct),
        "line_0_tax_code_id": str(gst),
        "line_0_quantity": "1",
        "line_0_unit_price": "100",
        "line_0_discount_pct": "0",
        "notes": "",
    }
    r = await client.post("/credit-notes", data=data, follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    assert r.headers["location"].startswith("/credit-notes/")


@pytest.mark.asyncio
async def test_credit_notes_detail_shows_draft_actions(
    client: AsyncClient,
) -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(acct, gst, Decimal("50.00"))],
        )
    r = await client.get(f"/credit-notes/{cn.id}")
    assert r.status_code == 200
    assert "DRAFT" in r.text
    assert "Edit" in r.text
    assert "Post" in r.text
    assert "Discard" in r.text


@pytest.mark.asyncio
async def test_credit_note_post_transitions_to_posted(
    client: AsyncClient,
) -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(acct, gst, Decimal("80.00"))],
        )
    r = await client.post(
        f"/credit-notes/{cn.id}/post", follow_redirects=False
    )
    assert r.status_code in (302, 303)
    detail = await client.get(f"/credit-notes/{cn.id}")
    assert detail.status_code == 200
    assert "POSTED" in detail.text
    assert "Void" in detail.text


@pytest.mark.asyncio
async def test_credit_note_archive_redirects(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(acct, gst, Decimal("10.00"))],
        )
    r = await client.post(
        f"/credit-notes/{cn.id}/archive", follow_redirects=False
    )
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/credit-notes"
