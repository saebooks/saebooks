"""Bad-debt recovery service tests (Task 5).

  - Recovery on a written-off invoice posts Dr bank / Cr 4-1290 Bad Debt
    Recovery (OTHER_INCOME), NO GST line, origin BAD_DEBT_RECOVERY.
  - Two partial recoveries both post (no cap to the original debt).
  - Recovery on a non-written-off invoice is rejected.
  - Recovery with a non-positive amount is rejected.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, JournalOrigin
from saebooks.models.tax_code import TaxCode
from saebooks.services import bad_debt as svc
from saebooks.services import invoices as inv_svc
from saebooks.services.bad_debt import BadDebtError

pytestmark = pytest.mark.postgres_only

_TODAY = date(2026, 5, 1)
_WO_DATE = date(2026, 6, 23)
_REC_DATE = date(2026, 7, 10)


async def _ctx():
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        income = (
            await session.execute(
                select(Account).where(Account.company_id == company.id, Account.code == "4-6000")
            )
        ).scalar_one()
        fre = (
            await session.execute(
                select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "FRE")
            )
        ).scalar_one()
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code.like("1-11%"),
                    Account.is_header.is_(False),
                )
            )
        ).scalars().first()
        assert bank is not None
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id, Contact.name == "Recovery Test Pty"
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id, name="Recovery Test Pty",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        return company.id, company.tenant_id, contact.id, income.id, fre.id, bank.id


async def _written_off_invoice(cid, tid, contact, income, fre, unit_price):
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session, company_id=cid, contact_id=contact,
            issue_date=_TODAY, due_date=_TODAY + timedelta(days=30),
            lines=[{
                "description": "Job", "account_id": income, "tax_code_id": fre,
                "quantity": Decimal("1"), "unit_price": unit_price,
                "discount_pct": Decimal("0"),
            }],
        )
        inv_id = inv.id
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv_id, posted_by="test")
    async with AsyncSessionLocal() as session:
        await svc.write_off_invoice(
            session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
            write_off_date=_WO_DATE, posted_by="test",
        )
    return inv_id


async def test_recovery_posts_other_income_no_gst():
    cid, tid, contact, income, fre, bank = await _ctx()
    inv_id = await _written_off_invoice(cid, tid, contact, income, fre, Decimal("500.00"))

    async with AsyncSessionLocal() as session:
        je = await svc.record_recovery(
            session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
            bank_account_id=bank, amount=Decimal("500.00"),
            recovery_date=_REC_DATE, posted_by="test",
        )
        je_id = je.id

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, je_id)
        assert je.status == EntryStatus.POSTED
        assert je.origin == JournalOrigin.BAD_DEBT_RECOVERY
        assert je.source_type == "invoice"
        assert je.source_id == inv_id
        assert je.entry_date == _REC_DATE
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == je_id)
            )
        ).scalars().all()
        # Exactly two legs — no GST line.
        assert len(lines) == 2
        debits = {}
        credits = {}
        recovery_acct_code = None
        for ln in lines:
            acct = await session.get(Account, ln.account_id)
            if ln.debit > 0:
                debits[acct.code] = ln.debit
            if ln.credit > 0:
                credits[acct.code] = ln.credit
                if acct.account_type == AccountType.OTHER_INCOME:
                    recovery_acct_code = acct.code
        # Dr bank 500, Cr 4-1290 Bad Debt Recovery 500.
        assert sum(debits.values()) == Decimal("500.00")
        assert credits.get("4-1290") == Decimal("500.00")
        assert recovery_acct_code == "4-1290"
        # No GST Collected (2-1310) anywhere.
        assert "2-1310" not in debits and "2-1310" not in credits


async def test_two_partial_recoveries_both_post():
    cid, tid, contact, income, fre, bank = await _ctx()
    inv_id = await _written_off_invoice(cid, tid, contact, income, fre, Decimal("1000.00"))

    async with AsyncSessionLocal() as session:
        je1 = await svc.record_recovery(
            session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
            bank_account_id=bank, amount=Decimal("300.00"),
            recovery_date=_REC_DATE, posted_by="test",
        )
        je1_id = je1.id
    async with AsyncSessionLocal() as session:
        je2 = await svc.record_recovery(
            session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
            bank_account_id=bank, amount=Decimal("250.00"),
            recovery_date=_REC_DATE + timedelta(days=30), posted_by="test",
        )
        je2_id = je2.id
    assert je1_id != je2_id

    async with AsyncSessionLocal() as session:
        for jid, amt in ((je1_id, Decimal("300.00")), (je2_id, Decimal("250.00"))):
            je = await session.get(JournalEntry, jid)
            assert je.origin == JournalOrigin.BAD_DEBT_RECOVERY
            lines = (
                await session.execute(
                    select(JournalLine).where(JournalLine.entry_id == jid)
                )
            ).scalars().all()
            assert sum((ln.credit for ln in lines), Decimal("0")) == amt


async def test_recovery_rejected_on_non_written_off():
    cid, tid, contact, income, fre, bank = await _ctx()
    # Posted but NOT written off.
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session, company_id=cid, contact_id=contact,
            issue_date=_TODAY, due_date=_TODAY + timedelta(days=30),
            lines=[{
                "description": "Job", "account_id": income, "tax_code_id": fre,
                "quantity": Decimal("1"), "unit_price": Decimal("200.00"),
                "discount_pct": Decimal("0"),
            }],
        )
        inv_id = inv.id
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv_id, posted_by="test")

    async with AsyncSessionLocal() as session:
        with pytest.raises(BadDebtError, match="not WRITTEN_OFF"):
            await svc.record_recovery(
                session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
                bank_account_id=bank, amount=Decimal("100.00"),
                recovery_date=_REC_DATE, posted_by="test",
            )


async def test_recovery_rejects_non_positive_amount():
    cid, tid, contact, income, fre, bank = await _ctx()
    inv_id = await _written_off_invoice(cid, tid, contact, income, fre, Decimal("100.00"))
    async with AsyncSessionLocal() as session:
        with pytest.raises(BadDebtError, match="must be positive"):
            await svc.record_recovery(
                session, company_id=cid, tenant_id=tid, invoice_id=inv_id,
                bank_account_id=bank, amount=Decimal("0.00"),
                recovery_date=_REC_DATE, posted_by="test",
            )
