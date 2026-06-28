"""Bad-debt write-off service tests (Task 3).

Covers:
  - GST-free (FRE) write-off: Dr 6-2050 / Cr 1-1200, no GST line.
  - Taxable write-off: Dr 6-2050 ex-GST + Dr 2-1310 GST / Cr 1-1200, GST
    reclaimed (decreasing adjustment).
  - Partial-balance write-off: only the unpaid balance is written off, GST
    reclaimed pro-rata.
  - Write-off settles the invoice (WRITTEN_OFF, amount_paid == total) and the
    invoice drops out of the aged-receivables report.
  - Hypothesis property test on the pure split function: the three legs always
    tie to the exact balance and never over-claim GST.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, JournalOrigin
from saebooks.models.payment import PaymentDirection
from saebooks.models.tax_code import TaxCode
from saebooks.services import bad_debt as svc
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as pay_svc
from saebooks.services import reports as reports_svc
from saebooks.services.bad_debt import BadDebtError, compute_write_off_split

pytestmark = pytest.mark.postgres_only

_TODAY = date(2026, 5, 1)
_WO_DATE = date(2026, 6, 23)


async def _ctx():
    """(company_id, tenant_id, contact_id, income_acct_id, gst_tc_id, fre_tc_id, bank_acct_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        income = (
            await session.execute(
                select(Account).where(Account.company_id == company.id, Account.code == "4-6000")
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "GST")
            )
        ).scalar_one()
        fre = (
            await session.execute(
                select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "FRE")
            )
        ).scalar_one()
        bank = (
            await session.execute(
                select(Account).where(Account.company_id == company.id, Account.code == "1-1110")
            )
        ).scalar_one_or_none()
        if bank is None:
            bank = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id, Account.code.like("1-11%")
                    )
                )
            ).scalars().first()
        assert bank is not None
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id, Contact.name == "Bad Debt Test Pty"
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Bad Debt Test Pty",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        return (
            company.id, company.tenant_id, contact.id, income.id, gst.id, fre.id, bank.id,
        )


async def _make_posted_invoice(cid, contact, income_acct, tax_code_id, unit_price):
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=_TODAY,
            due_date=_TODAY + timedelta(days=30),
            lines=[
                {
                    "description": "Job",
                    "account_id": income_acct,
                    "tax_code_id": tax_code_id,
                    "quantity": Decimal("1"),
                    "unit_price": unit_price,
                    "discount_pct": Decimal("0"),
                }
            ],
        )
        inv_id = inv.id
    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv_id, posted_by="test")
        return posted.id


async def _je_legs(je_id):
    """Return {code: (debit, credit)} aggregated by account code for a JE."""
    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == je_id)
            )
        ).scalars().all()
        debits: dict[str, Decimal] = {}
        credits: dict[str, Decimal] = {}
        for ln in lines:
            acct = await session.get(Account, ln.account_id)
            if ln.debit > 0:
                debits[acct.code] = debits.get(acct.code, Decimal("0")) + ln.debit
            if ln.credit > 0:
                credits[acct.code] = credits.get(acct.code, Decimal("0")) + ln.credit
        return debits, credits


@pytest.mark.asyncio
async def test_gst_free_write_off():
    cid, tid, contact, income, _gst, fre, _bank = await _ctx()
    inv_id = await _make_posted_invoice(cid, contact, income, fre, Decimal("556.10"))

    async with AsyncSessionLocal() as session:
        inv = await svc.write_off_invoice(
            session,
            company_id=cid,
            tenant_id=tid,
            invoice_id=inv_id,
            write_off_date=_WO_DATE,
            posted_by="test",
        )
        assert inv.status == InvoiceStatus.WRITTEN_OFF
        assert inv.amount_paid == inv.total == Decimal("556.10")
        je_id = inv.write_off_journal_entry_id
        assert je_id is not None

    debits, credits = await _je_legs(je_id)
    # Dr Bad Debts full balance, Cr AR full balance, NO GST line.
    assert debits.get("6-2050") == Decimal("556.10")
    assert credits.get("1-1200") == Decimal("556.10")
    assert "2-1310" not in debits
    assert sum(debits.values()) == sum(credits.values()) == Decimal("556.10")

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, je_id)
        assert je.status == EntryStatus.POSTED
        assert je.origin == JournalOrigin.BAD_DEBT_WRITEOFF
        assert je.source_type == "invoice"
        assert je.source_id == inv_id
        assert je.entry_date == _WO_DATE


@pytest.mark.asyncio
async def test_taxable_write_off_reclaims_gst():
    cid, tid, contact, income, gst, _fre, _bank = await _ctx()
    # $1000 ex-GST + $100 GST = $1100 gross.
    inv_id = await _make_posted_invoice(cid, contact, income, gst, Decimal("1000.00"))

    async with AsyncSessionLocal() as session:
        inv = await svc.write_off_invoice(
            session,
            company_id=cid,
            tenant_id=tid,
            invoice_id=inv_id,
            write_off_date=_WO_DATE,
            posted_by="test",
        )
        assert inv.status == InvoiceStatus.WRITTEN_OFF
        assert inv.total == Decimal("1100.00")
        je_id = inv.write_off_journal_entry_id

    debits, credits = await _je_legs(je_id)
    assert debits.get("6-2050") == Decimal("1000.00")  # ex-GST to Bad Debts
    assert debits.get("2-1310") == Decimal("100.00")   # GST reclaimed
    assert credits.get("1-1200") == Decimal("1100.00")  # gross cleared from AR
    assert sum(debits.values()) == sum(credits.values()) == Decimal("1100.00")


@pytest.mark.asyncio
async def test_partial_balance_write_off():
    cid, tid, contact, income, gst, _fre, bank = await _ctx()
    # $1100 gross; pay $440 first, write off the remaining $660.
    inv_id = await _make_posted_invoice(cid, contact, income, gst, Decimal("1000.00"))
    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            bank_account_id=bank,
            payment_date=_TODAY + timedelta(days=5),
            amount=Decimal("440.00"),
            direction=PaymentDirection.INCOMING,
        )
        pay_id = pay.id
    async with AsyncSessionLocal() as session:
        await pay_svc.post_payment(session, pay_id, posted_by="test")
    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session, pay_id,
            invoice_allocations=[(inv_id, Decimal("440.00"))],
        )

    async with AsyncSessionLocal() as session:
        inv = await svc.write_off_invoice(
            session,
            company_id=cid,
            tenant_id=tid,
            invoice_id=inv_id,
            write_off_date=_WO_DATE,
            posted_by="test",
        )
        assert inv.status == InvoiceStatus.WRITTEN_OFF
        assert inv.amount_paid == inv.total  # settled
        je_id = inv.write_off_journal_entry_id

    debits, credits = await _je_legs(je_id)
    # Balance written off = 660. GST reclaimed pro-rata = 100 * (660/1100) = 60.
    # Ex-GST to Bad Debts = 600.
    assert credits.get("1-1200") == Decimal("660.00")
    assert debits.get("2-1310") == Decimal("60.00")
    assert debits.get("6-2050") == Decimal("600.00")
    assert sum(debits.values()) == sum(credits.values()) == Decimal("660.00")


@pytest.mark.asyncio
async def test_write_off_drops_from_aged_receivables():
    cid, tid, contact, income, gst, _fre, _bank = await _ctx()
    inv_id = await _make_posted_invoice(cid, contact, income, gst, Decimal("1000.00"))

    # Before write-off: the invoice is in the aged-AR report.
    async with AsyncSessionLocal() as session:
        report = await reports_svc.aged_ar(session, cid, as_at=_WO_DATE)
        all_ids_before = {
            row.invoice_id for g in report.groups for row in g.invoices
        }
        assert inv_id in all_ids_before

    async with AsyncSessionLocal() as session:
        await svc.write_off_invoice(
            session,
            company_id=cid,
            tenant_id=tid,
            invoice_id=inv_id,
            write_off_date=_WO_DATE,
            posted_by="test",
        )

    # After write-off: gone from aged AR.
    async with AsyncSessionLocal() as session:
        report = await reports_svc.aged_ar(session, cid, as_at=_WO_DATE)
        all_ids_after = {
            row.invoice_id for g in report.groups for row in g.invoices
        }
        assert inv_id not in all_ids_after


@pytest.mark.asyncio
async def test_write_off_rejects_already_written_off():
    cid, tid, contact, income, _gst, fre, _bank = await _ctx()
    inv_id = await _make_posted_invoice(cid, contact, income, fre, Decimal("100.00"))
    async with AsyncSessionLocal() as session:
        await svc.write_off_invoice(
            session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
            write_off_date=_WO_DATE, posted_by="test",
        )
    async with AsyncSessionLocal() as session:
        with pytest.raises(BadDebtError, match="already written off"):
            await svc.write_off_invoice(
                session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
                write_off_date=_WO_DATE, posted_by="test",
            )


# ---------------------------------------------------------------------------
# Property tests (Hypothesis) — pure money split. CONTRIBUTING.md requires
# Hypothesis for money arithmetic.
# ---------------------------------------------------------------------------

@settings(max_examples=300, deadline=None)
@given(
    ex_gst=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50000.00"), places=2),
    gst=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("5000.00"), places=2),
    paid_fraction=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("0.99"), places=2),
)
def test_split_balances_and_never_overclaims_gst(ex_gst, gst, paid_fraction):
    total = ex_gst + gst
    amount_paid = (total * paid_fraction).quantize(Decimal("0.01"))
    if total - amount_paid <= Decimal("0"):
        return  # nothing to write off — service raises; not this property
    split = compute_write_off_split(
        total=total,
        amount_paid=amount_paid,
        line_subtotals=[ex_gst],
        line_taxes=[gst],
    )
    # 1. The three legs tie to the exact balance.
    assert split.ex_gst + split.gst == split.balance
    assert split.balance == total - amount_paid
    # 2. Never reclaim more GST than was charged, never negative.
    assert Decimal("0") <= split.gst <= gst
    # 3. Ex-GST leg is non-negative.
    assert split.ex_gst >= Decimal("0")


def test_split_rejects_fully_paid():
    with pytest.raises(BadDebtError):
        compute_write_off_split(
            total=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            line_subtotals=[Decimal("100.00")],
            line_taxes=[Decimal("0.00")],
        )


@pytest.mark.asyncio
async def test_future_dated_write_off_stays_open_until_write_off_date():
    """A write-off dated in the FUTURE keeps the invoice in aged AR until that
    date (matching the date-aware Trade Debtors control), then drops it on the
    write-off date. Guards the year-end (e.g. 30-Jun booked early) case."""
    cid, tid, contact, income, _gst, fre, _bank = await _ctx()
    inv_id = await _make_posted_invoice(cid, contact, income, fre, Decimal("1234.00"))

    # Write the invoice off effective _WO_DATE (booked ahead of its date).
    async with AsyncSessionLocal() as session:
        await svc.write_off_invoice(
            session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
            write_off_date=_WO_DATE, posted_by="test",
        )

    day_before = _WO_DATE - timedelta(days=1)

    # As at the day BEFORE the write-off date: still receivable → present.
    async with AsyncSessionLocal() as session:
        report = await reports_svc.aged_ar(session, cid, as_at=day_before)
        ids = {row.invoice_id for g in report.groups for row in g.invoices}
        assert inv_id in ids, "future-dated write-off must remain open before its date"

    # As at the write-off date itself: now off-ledger → gone.
    async with AsyncSessionLocal() as session:
        report = await reports_svc.aged_ar(session, cid, as_at=_WO_DATE)
        ids = {row.invoice_id for g in report.groups for row in g.invoices}
        assert inv_id not in ids, "write-off effective on its date must drop the invoice"
