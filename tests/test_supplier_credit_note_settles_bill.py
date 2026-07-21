"""Supplier credit note settles the bill it relieves (original_bill_id).

Mirror of ``test_credit_note_settles_invoice.py`` on the AP side. Prior to
this fix, ``_refresh_bill_amount_paid`` summed ``PaymentAllocation`` rows
only -- a posted supplier credit note linked to a bill via
``original_bill_id`` never reduced the bill's ``amount_paid``, so the bill
stayed fully outstanding in aged payables even after the supplier had
credited us in full.

Unlike the customer-side ``CreditNote`` (which nets ``total -
amount_allocated`` because a CN can be cash-refunded), ``SupplierCreditNote``
has no ``amount_allocated`` / cash-refund path in this codebase (no
``payment_allocations.supplier_credit_note_id`` column) -- so a posted SCN's
FULL total always relieves the bill it is linked to.

Covers:
  (a) post bill + SCN (full amount, original_bill_id set) ->
      bill.amount_paid == total, drops out of aged_ap.
  (b) void SCN -> amount_paid back to 0, reappears in aged_ap.
  (c) partial SCN (< bill total) -> partial reduction, bill stays open for
      the remainder.
  (d) SCN + payment together covering the bill -> amount_paid == total,
      no double-count, drops out of aged_ap.
  (e) the SCN's own GL journal is unchanged and books balance.
  (f) an SCN linked to one bill does not leak relief onto another bill in
      the same company (bill-scoping probe on the new query).
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal, Base
from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.payment import Payment, PaymentDirection
from saebooks.models.supplier_credit_note import (
    SupplierCreditNote,
    SupplierCreditNoteStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bill_svc
from saebooks.services import payments as pay_svc
from saebooks.services import reports as report_svc
from saebooks.services import supplier_credit_notes as scn_svc

pytestmark = pytest.mark.postgres_only

_COUNTER_PREFIXES = {"bill": "BILL-", "supplier_credit_note": "SCN-", "payment": "PAY-"}


async def _fast_forward_counter(kind: str, model_cls: type[Base]) -> None:
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
                select(model_cls.number).where(  # type: ignore[attr-defined]
                    model_cls.company_id == company.id,  # type: ignore[attr-defined]
                    model_cls.number.isnot(None),  # type: ignore[attr-defined]
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
                    DocumentCounter.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind=kind,
                prefix=_COUNTER_PREFIXES[kind],
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


@pytest.fixture(autouse=True, scope="module")
async def _prep_counters() -> AsyncGenerator[None, None]:
    await _fast_forward_counter("bill", Bill)
    await _fast_forward_counter("supplier_credit_note", SupplierCreditNote)
    await _fast_forward_counter("payment", Payment)
    yield


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, bank_acct_id, expense_acct_id, gst_id)."""
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
                    Account.company_id == company.id, Account.code == "1-1110"
                )
            )
        ).scalar_one()
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "6-1000"
                )
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id, TaxCode.code == "GST"
                )
            )
        ).scalar_one()

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test SCN-Settles Supplier",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test SCN-Settles Supplier",
                contact_type=ContactType.SUPPLIER,
                email="scnsettle@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, bank.id, expense.id, gst.id


async def _post_bill(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    expense: uuid.UUID,
    gst: uuid.UUID,
    amount: Decimal,
    *,
    issue_date: date = date(2026, 4, 20),
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=issue_date,
            due_date=issue_date + timedelta(days=30),
            lines=[
                {
                    "description": "Bill line",
                    "account_id": expense,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        await bill_svc.post_bill(session, bill.id, posted_by="test")
    return bill.id


async def _post_scn(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    expense: uuid.UUID,
    gst: uuid.UUID,
    amount: Decimal,
    *,
    original_bill_id: uuid.UUID | None,
    issue_date: date = date(2026, 4, 21),
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        scn = await scn_svc.api_create(
            session,
            company_id=company_id,
            tenant_id=(await session.get(Company, company_id)).tenant_id,
            actor="test",
            contact_id=contact_id,
            issue_date=issue_date,
            lines=[
                {
                    "description": "Materials refund",
                    "account_id": expense,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                }
            ],
            original_bill_id=original_bill_id,
        )
    async with AsyncSessionLocal() as session:
        posted = await scn_svc.api_post(
            session, scn.id, "test", scn.version,
            tenant_id=scn.tenant_id, company_id=company_id,
        )
    return posted.id


async def _is_in_aged_ap(company_id: uuid.UUID, bill_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session:
        report = await report_svc.aged_ap(
            session, company_id, as_at=date(2026, 6, 1)
        )
        for group in report.groups:
            for row in group.invoices:
                if row.invoice_id == bill_id:
                    return True
    return False


@pytest.mark.asyncio
async def test_posted_scn_settles_bill_and_clears_aging() -> None:
    cid, contact, _bank, expense, gst = await _ctx()
    bill_id = await _post_bill(
        cid, contact, expense, gst, Decimal("100.00")
    )  # total = 110.00 incl GST

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        total = bill.total
    assert total == Decimal("110.00")
    assert await _is_in_aged_ap(cid, bill_id) is True

    await _post_scn(
        cid, contact, expense, gst, Decimal("100.00"),
        original_bill_id=bill_id,
    )  # SCN total = 110.00

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        assert bill.amount_paid == total
        assert bill.total - bill.amount_paid == Decimal("0.00")

    assert await _is_in_aged_ap(cid, bill_id) is False


@pytest.mark.asyncio
async def test_voiding_scn_reopens_bill() -> None:
    cid, contact, _bank, expense, gst = await _ctx()
    bill_id = await _post_bill(cid, contact, expense, gst, Decimal("100.00"))
    scn_id = await _post_scn(
        cid, contact, expense, gst, Decimal("100.00"),
        original_bill_id=bill_id,
    )

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        assert bill.amount_paid == Decimal("110.00")
    assert await _is_in_aged_ap(cid, bill_id) is False

    async with AsyncSessionLocal() as session:
        scn = await session.get(SupplierCreditNote, scn_id)
        assert scn is not None
        await scn_svc.void_supplier_credit_note(session, scn_id, posted_by="test")

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        assert bill.amount_paid == Decimal("0.00")

    assert await _is_in_aged_ap(cid, bill_id) is True


@pytest.mark.asyncio
async def test_partial_scn_reduces_bill_partially() -> None:
    cid, contact, _bank, expense, gst = await _ctx()
    bill_id = await _post_bill(
        cid, contact, expense, gst, Decimal("200.00")
    )  # total = 220.00

    await _post_scn(
        cid, contact, expense, gst, Decimal("50.00"),  # SCN total = 55.00
        original_bill_id=bill_id,
    )

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        assert bill.amount_paid == Decimal("55.00")
        assert bill.total - bill.amount_paid == Decimal("165.00")

    # Bill is still open -- the partial CN does not fully clear it.
    assert await _is_in_aged_ap(cid, bill_id) is True


@pytest.mark.asyncio
async def test_scn_plus_payment_covers_bill_no_double_count() -> None:
    cid, contact, bank, expense, gst = await _ctx()
    bill_id = await _post_bill(
        cid, contact, expense, gst, Decimal("200.00")
    )  # total = 220.00

    await _post_scn(
        cid, contact, expense, gst, Decimal("100.00"),  # SCN total = 110.00
        original_bill_id=bill_id,
    )

    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=date(2026, 4, 22),
            amount=Decimal("110.00"),
            direction=PaymentDirection.OUTGOING,
        )
    async with AsyncSessionLocal() as session:
        await pay_svc.post_payment(session, pay.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session, pay.id, bill_allocations=[(bill_id, Decimal("110.00"))]
        )

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, bill_id)
        assert bill.amount_paid == Decimal("220.00")
        assert bill.total - bill.amount_paid == Decimal("0.00")
        # Capped at total -- no double-count beyond what was owed.
        assert bill.amount_paid <= bill.total

    assert await _is_in_aged_ap(cid, bill_id) is False


@pytest.mark.asyncio
async def test_scn_journal_unchanged_and_books_balance() -> None:
    cid, contact, _bank, expense, gst = await _ctx()
    bill_id = await _post_bill(cid, contact, expense, gst, Decimal("100.00"))
    scn_id = await _post_scn(
        cid, contact, expense, gst, Decimal("100.00"),
        original_bill_id=bill_id,
    )

    async with AsyncSessionLocal() as session:
        scn = await session.get(SupplierCreditNote, scn_id)
        assert scn is not None
        assert scn.status == SupplierCreditNoteStatus.POSTED
        assert scn.journal_entry_id is not None

        entry = await session.get(JournalEntry, scn.journal_entry_id)
        assert entry is not None
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        ).scalars().all()
        debits = sum((ln.debit for ln in lines), Decimal("0"))
        credits = sum((ln.credit for ln in lines), Decimal("0"))
        assert debits == credits  # SCN journal balances
        assert debits == Decimal("110.00")  # Dr AP 110 = Cr expense 100 + Cr GST 10


@pytest.mark.asyncio
async def test_scn_relief_does_not_leak_onto_another_bill() -> None:
    """Bill-scoping probe: an SCN's ``original_bill_id`` filter must not
    relieve any bill other than the one it is linked to -- the new query
    added to ``_refresh_bill_amount_paid`` / ``_bill_settled_asof`` filters
    strictly by FK, so a second, unrelated bill in the same company must be
    completely unaffected.
    """
    cid, contact, _bank, expense, gst = await _ctx()
    bill_a = await _post_bill(cid, contact, expense, gst, Decimal("100.00"))
    bill_b = await _post_bill(cid, contact, expense, gst, Decimal("300.00"))

    await _post_scn(
        cid, contact, expense, gst, Decimal("100.00"),
        original_bill_id=bill_a,
    )

    async with AsyncSessionLocal() as session:
        a = await bill_svc.get(session, bill_a)
        b = await bill_svc.get(session, bill_b)
        assert a.amount_paid == Decimal("110.00")
        assert b.amount_paid == Decimal("0.00")

    assert await _is_in_aged_ap(cid, bill_a) is False
    assert await _is_in_aged_ap(cid, bill_b) is True
