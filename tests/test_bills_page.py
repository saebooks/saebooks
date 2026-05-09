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
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.services import bills as svc


async def _fast_forward_bill_counter() -> None:
    """Advance the per-company bill DocumentCounter past any existing
    BILL-NNNNNN number already in the DB — see ``test_bills.py`` for
    the full rationale.
    """
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        numbers = (
            await session.execute(
                select(Bill.number).where(
                    Bill.company_id == company.id,
                    Bill.number.isnot(None),
                )
            )
        ).scalars().all()
        max_suffix = 0
        for n in numbers:
            try:
                max_suffix = max(max_suffix, int(str(n).rsplit("-", 1)[-1]))
            except ValueError:
                continue
        counter = (
            await session.execute(
                select(DocumentCounter).where(
                    DocumentCounter.company_id == company.id,
                    DocumentCounter.kind == "bill",
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind="bill",
                prefix="BILL-",
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


@pytest.fixture(autouse=True, scope="module")
async def _prep_bill_counter() -> AsyncGenerator[None, None]:
    await _fast_forward_bill_counter()
    yield


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


@pytest.mark.asyncio
async def test_bills_create_rejects_cross_tenant_fks(client: AsyncClient) -> None:
    """CIVL-1: POST /bills must reject contact_id, account_id, tax_code_id
    that belong to a foreign tenant (cross-tenant write gap on AP lane)."""
    _cid, own_contact, own_acct, own_gst = await _ctx()
    today = date(2026, 4, 20)

    # Seed a second isolated tenant/company so each FK is a valid DB row
    # that belongs to a *different* company from the one the web handler
    # resolves via _first_company().
    foreign_tid = uuid.uuid4()
    foreign_cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Tenant(
            id=foreign_tid,
            name=f"ForeignCo-CIVL1-{foreign_tid.hex[:6]}",
            slug=f"civl1-{foreign_tid.hex[:6]}",
        ))
        await session.flush()
        session.add(Company(
            id=foreign_cid,
            tenant_id=foreign_tid,
            name=f"Foreign Corp CIVL1 {foreign_tid.hex[:6]}",
        ))
        await session.flush()
        f_contact = Contact(
            company_id=foreign_cid, tenant_id=foreign_tid,
            name="Foreign Supplier CIVL1",
            contact_type=ContactType.SUPPLIER,
        )
        f_acct = Account(
            company_id=foreign_cid, tenant_id=foreign_tid,
            code=f"6-{foreign_tid.hex[:4]}",
            name="Foreign Expense CIVL1",
            account_type=AccountType.EXPENSE,
            is_header=False,
        )
        f_tc = TaxCode(
            company_id=foreign_cid, tenant_id=foreign_tid,
            code=f"G{foreign_tid.hex[:3]}",
            name="Foreign GST CIVL1",
            rate=Decimal("10"),
        )
        session.add_all([f_contact, f_acct, f_tc])
        await session.commit()
        await session.refresh(f_contact)
        await session.refresh(f_acct)
        await session.refresh(f_tc)

    base_data: dict[str, str] = {
        "contact_id": str(own_contact),
        "issue_date": today.isoformat(),
        "due_date": (today + timedelta(days=30)).isoformat(),
        "line_0_description": "CIVL1 cross-tenant test",
        "line_0_account_id": str(own_acct),
        "line_0_tax_code_id": str(own_gst),
        "line_0_quantity": "1",
        "line_0_unit_price": "100",
        "line_0_discount_pct": "0",
    }

    r = await client.post(
        "/bills",
        data={**base_data, "contact_id": str(f_contact.id)},
        follow_redirects=False,
    )
    assert r.status_code == 422, f"foreign contact_id: expected 422, got {r.status_code}"

    r = await client.post(
        "/bills",
        data={**base_data, "line_0_account_id": str(f_acct.id)},
        follow_redirects=False,
    )
    assert r.status_code == 422, f"foreign account_id: expected 422, got {r.status_code}"

    r = await client.post(
        "/bills",
        data={**base_data, "line_0_tax_code_id": str(f_tc.id)},
        follow_redirects=False,
    )
    assert r.status_code == 422, f"foreign tax_code_id: expected 422, got {r.status_code}"
