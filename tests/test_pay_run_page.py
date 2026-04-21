"""Router smoke tests for ``/pay-run``.

* Index renders with 200 even on an empty DB (shows empty states)
* Index shows a posted bill + a bank-detail-populated remitter
* POST /pay-run/export returns a text/plain attachment with the
  expected Content-Disposition and the file body looks like an
  ABA file (starts with '0', ends with '7', each line 120 chars).
* POST rejects selections with no amount set / missing bill id.
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
from saebooks.services import bills as bill_svc


async def _fast_forward_bill_counter() -> None:
    """Advance the per-company bill DocumentCounter past any existing
    BILL-NNNNNN number — see ``test_bills.py`` for the full rationale.
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


async def _bootstrap() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, bank_id, expense_id, supplier_id) with ABA fields."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "1-1180",
                )
            )
        ).scalars().first()
        if bank is None:
            bank = Account(
                company_id=company.id,
                code="1-1180",
                name="Pay-run bank",
                account_type=AccountType.ASSET,
                reconcile=True,
            )
            session.add(bank)
            await session.commit()
            await session.refresh(bank)
        bank.bsb = "062-000"
        bank.bank_account_number = "11112222"
        bank.bank_account_title = "SAE ENGINEERING"
        bank.apca_user_id = "301500"
        bank.bank_abbreviation = "CBA"
        await session.commit()

        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "6-1000",
                )
            )
        ).scalar_one()

        supplier = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Pay-run test supplier",
                )
            )
        ).scalars().first()
        if supplier is None:
            supplier = Contact(
                company_id=company.id,
                name="Pay-run test supplier",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(supplier)
            await session.commit()
            await session.refresh(supplier)
        supplier.bank_bsb = "062-001"
        supplier.bank_account_number = "87654321"
        supplier.bank_account_title = "PAYRUN TEST SUPPLIER"
        await session.commit()

        return company.id, bank.id, expense.id, supplier.id


async def _post_bill(
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
    expense_id: uuid.UUID,
    *,
    total: Decimal,
) -> uuid.UUID:
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=supplier_id,
            issue_date=today,
            due_date=today + timedelta(days=14),
            lines=[
                {
                    "description": "Pay-run test line",
                    "account_id": expense_id,
                    "tax_code_id": None,
                    "quantity": Decimal("1"),
                    "unit_price": total,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        posted = await bill_svc.post_bill(session, bill.id, posted_by="tests")
    return posted.id


@pytest.mark.asyncio
async def test_pay_run_index_renders(client: AsyncClient) -> None:
    await _bootstrap()
    r = await client.get("/pay-run")
    assert r.status_code == 200
    assert "Pay run" in r.text


@pytest.mark.asyncio
async def test_pay_run_index_lists_posted_bill(client: AsyncClient) -> None:
    cid, _bank, expense, supplier = await _bootstrap()
    await _post_bill(cid, supplier, expense, total=Decimal("42.00"))
    r = await client.get("/pay-run")
    assert r.status_code == 200
    assert "Pay-run test supplier" in r.text
    assert "42.00" in r.text


@pytest.mark.asyncio
async def test_export_returns_aba_attachment(client: AsyncClient) -> None:
    cid, bank, expense, supplier = await _bootstrap()
    bill_id = await _post_bill(
        cid, supplier, expense, total=Decimal("123.45")
    )
    data = {
        "bank_account_id": str(bank),
        "process_date": "2026-04-21",
        "description": "CREDITORS",
        f"select_{bill_id}": "on",
        f"amount_{bill_id}": "123.45",
    }
    r = await client.post("/pay-run/export", data=data)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert "attachment" in r.headers["content-disposition"]
    assert "aba-260421" in r.headers["content-disposition"]

    lines = r.text.rstrip("\r\n").split("\r\n")
    assert lines[0].startswith("0")
    assert lines[-1].startswith("7")
    # Header + one detail + trailer.
    assert len(lines) == 3
    assert all(len(ln) == 120 for ln in lines)
    # Amount encoded as 12345 cents in positions 21-30 of the detail.
    assert lines[1][20:30] == "0000012345"


@pytest.mark.asyncio
async def test_export_rejects_empty_selection(client: AsyncClient) -> None:
    _cid, bank, _expense, _supplier = await _bootstrap()
    data = {
        "bank_account_id": str(bank),
        "process_date": "2026-04-21",
        "description": "CREDITORS",
    }
    r = await client.post("/pay-run/export", data=data)
    assert r.status_code == 400
    assert "Select at least one" in r.text
