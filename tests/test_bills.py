"""Tests for ``saebooks.services.bills``.

Mirror of ``test_invoices.py`` — walks the full AP bill lifecycle:

1. ``create_draft`` computes line totals correctly with add-on GST.
2. Tax-free lines produce zero line_tax.
3. Discount percent applies correctly.
4. ``update_draft`` replaces lines and recalcs.
5. ``post_bill`` mints the number via numbering, posts the GL journal
   (Dr expense + auto-GST Paid + Cr AP), stamps the bill row.
6. ``void_bill`` reverses the journal and marks VOIDED.
7. Posting an empty bill raises BillError.
8. Posting a second time raises BillError.
9. Void of an unposted DRAFT flips to VOIDED without touching GL.
10. Editing a POSTED bill is blocked.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as svc


async def _fast_forward_bill_counter() -> None:
    """Advance the per-company bill DocumentCounter past any existing
    BILL-NNNNNN number already in the DB.

    The dev DB is persistent — prior test runs + real UI clicks can
    leave the counter behind the highest extant bill number, which
    causes ``IntegrityError: uq_bills_company_number`` when a new
    test tries to post a bill.
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


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, expense_acct_id, gst_tc_id, fre_tc_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        expense_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "6-1000",
                )
            )
        ).scalar_one()

        gst_tc = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "GST",
                )
            )
        ).scalar_one()
        fre_tc = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "FRE",
                )
            )
        ).scalar_one()

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test Bills Supplier Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test Bills Supplier Ltd",
                contact_type=ContactType.SUPPLIER,
                email="supplier@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, expense_acct.id, gst_tc.id, fre_tc.id


@pytest.mark.asyncio
async def test_create_draft_computes_line_totals() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            supplier_reference="SUP-12345",
            lines=[
                {
                    "description": "Website hosting",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("500"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    assert bill.status == BillStatus.DRAFT
    assert bill.number is None
    assert bill.supplier_reference == "SUP-12345"
    assert bill.subtotal == Decimal("500.00")
    assert bill.tax_total == Decimal("50.00")
    assert bill.total == Decimal("550.00")
    assert len(bill.lines) == 1


@pytest.mark.asyncio
async def test_tax_free_line_has_zero_tax() -> None:
    cid, contact, acct, _gst, fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {
                    "description": "GST-free groceries",
                    "account_id": acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("80"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    assert bill.tax_total == Decimal("0.00")
    assert bill.total == Decimal("80.00")


@pytest.mark.asyncio
async def test_discount_applies() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {
                    "description": "Bulk discount",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("4"),
                    "unit_price": Decimal("50"),
                    "discount_pct": Decimal("25"),
                }
            ],
        )
    # 4 * 50 * 0.75 = 150; +10% = 165
    assert bill.subtotal == Decimal("150.00")
    assert bill.tax_total == Decimal("15.00")
    assert bill.total == Decimal("165.00")


@pytest.mark.asyncio
async def test_update_draft_replaces_lines() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {"description": "Orig", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "100", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        updated = await svc.update_draft(
            session, bill.id,
            lines=[
                {"description": "Replace A", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "200", "discount_pct": "0"},
                {"description": "Replace B", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "50",  "discount_pct": "0"},
            ],
        )
    assert len(updated.lines) == 2
    assert updated.subtotal == Decimal("250.00")
    assert updated.total == Decimal("275.00")


@pytest.mark.asyncio
async def test_post_bill_mints_number_and_journal() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            supplier_reference="SUPINV-77",
            lines=[
                {"description": "Cloud hosting", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "1000", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_bill(session, bill.id, posted_by="test")
    assert posted.status == BillStatus.POSTED
    assert posted.number is not None
    assert posted.number.startswith("BILL-")
    assert posted.journal_entry_id is not None

    # Journal balances: Dr Expense 1000, Dr GST Paid 100, Cr AP 1100.
    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        assert je.status == EntryStatus.POSTED
        await session.refresh(je, ["lines"])
        total_dr = sum((ln.debit for ln in je.lines), Decimal("0"))
        total_cr = sum((ln.credit for ln in je.lines), Decimal("0"))
        assert total_dr == total_cr
        assert total_cr == Decimal("1100.00")
        # Expect at least 3 lines: expense, GST Paid (auto), AP.
        assert len(je.lines) >= 3


@pytest.mark.asyncio
async def test_void_posted_bill_reverses_journal() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {"description": "X", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "500", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        await svc.post_bill(session, bill.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        voided = await svc.void_bill(session, bill.id, posted_by="test")
    assert voided.status == BillStatus.VOIDED
    assert voided.void_journal_entry_id is not None


@pytest.mark.asyncio
async def test_void_draft_flips_status_without_journal() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {"description": "Draft-only", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "25", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        voided = await svc.void_bill(session, bill.id)
    assert voided.status == BillStatus.VOIDED
    assert voided.void_journal_entry_id is None


@pytest.mark.asyncio
async def test_cannot_post_empty_bill() -> None:
    cid, contact, _acct, _gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[],
        )
    with pytest.raises(svc.BillError, match="no lines"):
        async with AsyncSessionLocal() as session:
            await svc.post_bill(session, bill.id)


@pytest.mark.asyncio
async def test_cannot_post_twice() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {"description": "X", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "10", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        await svc.post_bill(session, bill.id)
    with pytest.raises(svc.BillError, match="already posted"):
        async with AsyncSessionLocal() as session:
            await svc.post_bill(session, bill.id)


@pytest.mark.asyncio
async def test_cannot_edit_posted_bill() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {"description": "X", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "10", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        await svc.post_bill(session, bill.id)
    with pytest.raises(svc.BillError, match="Cannot edit"):
        async with AsyncSessionLocal() as session:
            await svc.update_draft(session, bill.id, notes="nope")
