"""Credit note settles the invoice it relieves (original_invoice_id).

A posted customer credit note linked to an invoice via
``original_invoice_id`` must settle that invoice's outstanding balance so
a bad-debt write-off drops the invoice out of aged receivables. Voiding
the credit note reverts the invoice to open. A credit note that has been
cash-refunded (``amount_allocated == total``) must NOT settle the
invoice — its value left as cash, not as relief of AR.

Covers:
  (a) post invoice + CN (expense-coded, original_invoice_id set) →
      invoice.amount_paid == total, drops out of aged_ar.
  (b) void CN → amount_paid back to 0, reappears in aged_ar.
  (c) CN with amount_allocated == total does NOT settle the invoice.
  (d) the CN's own GL journal is unchanged and books balance.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal, Base
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.credit_note import CreditNote, CreditNoteStatus
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.invoice import Invoice
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.services import credit_notes as cn_svc
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as pay_svc
from saebooks.services import reports as report_svc

_COUNTER_PREFIXES = {"invoice": "INV-", "credit_note": "CN-"}


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
    await _fast_forward_counter("invoice", Invoice)
    await _fast_forward_counter("credit_note", CreditNote)
    yield


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company, contact, income_acct, expense_acct, gst_code)."""
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
        # Any pure EXPENSE-type account (bad-debt write-offs are coded to
        # an expense). Look it up by type so we don't pin a specific code.
        expense = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.EXPENSE,
                    Account.archived_at.is_(None),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert expense is not None, "no EXPENSE account in seeded CoA"
        from saebooks.models.tax_code import TaxCode

        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "GST",
                )
            )
        ).scalar_one()

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test CN-Settles Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test CN-Settles Ltd",
                contact_type=ContactType.CUSTOMER,
                email="cnsettle@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, income.id, expense.id, gst.id


async def _post_invoice(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    income: uuid.UUID,
    gst: uuid.UUID,
    amount: Decimal,
) -> uuid.UUID:
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Line",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")
    return inv.id


async def _post_credit_note(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    expense: uuid.UUID,
    gst: uuid.UUID,
    amount: Decimal,
    *,
    original_invoice_id: uuid.UUID | None,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        cn = await cn_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=date(2026, 4, 21),
            original_invoice_id=original_invoice_id,
            lines=[
                {
                    "description": "Bad debt write-off",
                    "account_id": expense,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": amount,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await cn_svc.post_credit_note(session, cn.id, posted_by="test")
    return posted.id


async def _is_in_aged_ar(company_id: uuid.UUID, invoice_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session:
        report = await report_svc.aged_ar(
            session, company_id, as_at=date(2026, 6, 1)
        )
        for group in report.groups:
            for row in group.invoices:
                if row.invoice_id == invoice_id:
                    return True
    return False


@pytest.mark.asyncio
async def test_posted_credit_note_settles_invoice_and_clears_aging() -> None:
    cid, contact, income, expense, gst = await _ctx()
    invoice_id = await _post_invoice(
        cid, contact, income, gst, Decimal("100.00")
    )  # total = 110.00 incl GST

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, invoice_id)
        total = inv.total
    assert total == Decimal("110.00")
    assert await _is_in_aged_ar(cid, invoice_id) is True

    await _post_credit_note(
        cid, contact, expense, gst, Decimal("100.00"),
        original_invoice_id=invoice_id,
    )  # CN total = 110.00, amount_allocated = 0

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, invoice_id)
        assert inv.amount_paid == total
        assert inv.total - inv.amount_paid == Decimal("0.00")

    assert await _is_in_aged_ar(cid, invoice_id) is False


@pytest.mark.asyncio
async def test_voiding_credit_note_reopens_invoice() -> None:
    cid, contact, income, expense, gst = await _ctx()
    invoice_id = await _post_invoice(
        cid, contact, income, gst, Decimal("100.00")
    )
    cn_id = await _post_credit_note(
        cid, contact, expense, gst, Decimal("100.00"),
        original_invoice_id=invoice_id,
    )

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, invoice_id)
        assert inv.amount_paid == Decimal("110.00")
    assert await _is_in_aged_ar(cid, invoice_id) is False

    async with AsyncSessionLocal() as session:
        await cn_svc.void_credit_note(session, cn_id, posted_by="test")

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, invoice_id)
        assert inv.amount_paid == Decimal("0.00")

    assert await _is_in_aged_ar(cid, invoice_id) is True


@pytest.mark.asyncio
async def test_cash_refunded_credit_note_does_not_settle_invoice() -> None:
    cid, contact, income, expense, gst = await _ctx()
    invoice_id = await _post_invoice(
        cid, contact, income, gst, Decimal("100.00")
    )
    cn_id = await _post_credit_note(
        cid, contact, expense, gst, Decimal("100.00"),
        original_invoice_id=invoice_id,
    )

    # Simulate the credit note having been cash-refunded: its full value
    # was paid out, so amount_allocated == total. It must then contribute
    # 0 to the invoice's relief (no double counting against AR).
    async with AsyncSessionLocal() as session:
        cn = await session.get(CreditNote, cn_id)
        assert cn is not None
        cn.amount_allocated = cn.total
        await session.commit()
        await pay_svc._refresh_invoice_amount_paid(session, invoice_id)
        await session.commit()

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.get(session, invoice_id)
        assert inv.amount_paid == Decimal("0.00")

    assert await _is_in_aged_ar(cid, invoice_id) is True


@pytest.mark.asyncio
async def test_credit_note_journal_unchanged_and_books_balance() -> None:
    cid, contact, income, expense, gst = await _ctx()
    invoice_id = await _post_invoice(
        cid, contact, income, gst, Decimal("100.00")
    )
    cn_id = await _post_credit_note(
        cid, contact, expense, gst, Decimal("100.00"),
        original_invoice_id=invoice_id,
    )

    async with AsyncSessionLocal() as session:
        cn = await session.get(CreditNote, cn_id)
        assert cn is not None
        assert cn.status == CreditNoteStatus.POSTED
        assert cn.journal_entry_id is not None
        # amount_allocated stays untouched by the settlement recompute.
        assert cn.amount_allocated == Decimal("0")

        entry = await session.get(JournalEntry, cn.journal_entry_id)
        assert entry is not None
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        ).scalars().all()
        debits = sum((ln.debit for ln in lines), Decimal("0"))
        credits = sum((ln.credit for ln in lines), Decimal("0"))
        assert debits == credits  # CN journal balances
        assert debits == Decimal("110.00")  # expense 100 + GST 10 = Cr AR 110
