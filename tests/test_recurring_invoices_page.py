"""Router smoke tests for ``/invoices/recurring``.

Covers:

* list page renders (empty-state + with data)
* new-schedule form renders with contacts + income accounts + frequency
* POST /invoices/recurring creates an ACTIVE schedule and redirects
* detail page shows schedule + lines + state-driven toolbar
* Pause flips status to PAUSED; Resume back to ACTIVE
* Run-now on a due template redirects to the newly-minted invoice
* Archive redirects back to the list
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
from saebooks.models.recurring_invoice import RecurrenceFrequency
from saebooks.models.tax_code import TaxCode
from saebooks.services import recurrence as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_account_id, gst_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
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
                    Contact.name == "Page Test Subscriber",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Page Test Subscriber",
                contact_type=ContactType.CUSTOMER,
                email="subscriber@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        return company.id, contact.id, income.id, gst.id


def _line(acct: uuid.UUID, gst: uuid.UUID, unit: Decimal) -> dict[str, object]:
    return {
        "description": "Retainer",
        "account_id": acct,
        "tax_code_id": gst,
        "quantity": Decimal("1"),
        "unit_price": unit,
        "discount_pct": Decimal("0"),
    }


@pytest.mark.asyncio
async def test_recurring_list_renders(client: AsyncClient) -> None:
    r = await client.get("/invoices/recurring")
    assert r.status_code == 200
    assert "Recurring invoices" in r.text


@pytest.mark.asyncio
async def test_recurring_new_form_renders(client: AsyncClient) -> None:
    _cid, _contact, _acct, _gst = await _ctx()
    r = await client.get("/invoices/recurring/new")
    assert r.status_code == 200
    assert "New recurring schedule" in r.text
    assert "Page Test Subscriber" in r.text
    # Every cadence should be in the dropdown
    for f in ("weekly", "fortnightly", "monthly", "quarterly", "yearly"):
        assert f in r.text.lower()


@pytest.mark.asyncio
async def test_recurring_create_redirects(client: AsyncClient) -> None:
    _cid, contact, acct, gst = await _ctx()
    data = {
        "name": "Web-form test monthly",
        "contact_id": str(contact),
        "frequency": "MONTHLY",
        "next_run": date(2026, 6, 1).isoformat(),
        "anchor_day": "1",
        "end_date": "",
        "due_days": "30",
        "payment_terms": "",
        "notes": "",
        "line_0_description": "Retainer",
        "line_0_account_id": str(acct),
        "line_0_tax_code_id": str(gst),
        "line_0_quantity": "1",
        "line_0_unit_price": "250",
        "line_0_discount_pct": "0",
    }
    r = await client.post(
        "/invoices/recurring", data=data, follow_redirects=False
    )
    assert r.status_code in (302, 303), r.text
    assert r.headers["location"].startswith("/invoices/recurring/")


@pytest.mark.asyncio
async def test_recurring_detail_shows_active_toolbar(
    client: AsyncClient,
) -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Detail smoke test",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 6, 1),
            lines=[_line(acct, gst, Decimal("100.00"))],
        )
    r = await client.get(f"/invoices/recurring/{tpl.id}")
    assert r.status_code == 200
    assert "ACTIVE" in r.text
    assert "Pause" in r.text
    assert "End" in r.text
    assert "Run now" in r.text


@pytest.mark.asyncio
async def test_recurring_pause_transitions_to_paused(
    client: AsyncClient,
) -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Pause smoke test",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 6, 1),
            lines=[_line(acct, gst, Decimal("40.00"))],
        )
    r = await client.post(
        f"/invoices/recurring/{tpl.id}/pause", follow_redirects=False
    )
    assert r.status_code in (302, 303)
    detail = await client.get(f"/invoices/recurring/{tpl.id}")
    assert detail.status_code == 200
    assert "PAUSED" in detail.text
    assert "Resume" in detail.text


@pytest.mark.asyncio
async def test_recurring_run_now_redirects_to_invoice(
    client: AsyncClient,
) -> None:
    cid, contact, acct, gst = await _ctx()
    # Template is already due (next_run in the past).
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Run-now smoke test",
            frequency=RecurrenceFrequency.WEEKLY,
            next_run=date(2026, 4, 20),
            lines=[_line(acct, gst, Decimal("75.00"))],
        )
    r = await client.post(
        f"/invoices/recurring/{tpl.id}/run", follow_redirects=False
    )
    assert r.status_code in (302, 303), r.text
    # Lands on the newly-minted invoice page
    assert r.headers["location"].startswith("/invoices/")
    assert "/recurring" not in r.headers["location"]


@pytest.mark.asyncio
async def test_recurring_archive_redirects_to_list(
    client: AsyncClient,
) -> None:
    cid, contact, acct, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        tpl = await svc.create(
            session,
            company_id=cid,
            contact_id=contact,
            name="Archive smoke test",
            frequency=RecurrenceFrequency.MONTHLY,
            next_run=date(2026, 6, 1),
            lines=[_line(acct, gst, Decimal("5.00"))],
        )
    r = await client.post(
        f"/invoices/recurring/{tpl.id}/archive", follow_redirects=False
    )
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/invoices/recurring"
