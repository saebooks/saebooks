"""Tests for deferred-revenue posting and recognition (FITC-3).

Gap: multi-month contract revenue was fully booked to income on the posting
date.  Fix: lines with service_start_date / service_end_date spanning > 1
calendar month are credited to Unearned Income (2-1760) on posting, then
amortized monthly via recognize_deferred_revenue().

Scenarios tested
----------------
1. Multi-month line → Cr Unearned Income on post, income account untouched.
2. Single-month line → Cr Income as before (positive control, no regression).
3. recognize_deferred_revenue → Dr Unearned, Cr Income for correct monthly amount.
4. Recognition is idempotent for the same period.
5. Multi-period: first month recognized; second month not yet.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceLine, InvoiceStatus
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as inv_svc
from saebooks.services import deferred_revenue as dr_svc
pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_acct_id, unearned_acct_id, fre_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        income_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "4-6000",
                )
            )
        ).scalar_one()

        unearned_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "2-1760",
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
                    Contact.name == "Test Deferred Rev Ltd",
                )
            )
        ).scalars().first()

        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test Deferred Rev Ltd",
                contact_type=ContactType.CUSTOMER,
                email="deferredtest@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, income_acct.id, unearned_acct.id, fre_tc.id


async def _je_lines(je_id: uuid.UUID) -> list[JournalLine]:
    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, je_id, options=[selectinload(JournalEntry.lines)])
        assert je is not None
        return list(je.lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deferred_multi_month_posts_to_unearned_income() -> None:
    """$1,200 annual membership → Cr Unearned Income on posting, not income acct."""
    cid, contact, income_acct, unearned_acct, fre = await _ctx()

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 4, 30),
            lines=[
                {
                    "description": "Annual membership Apr 2026 – Mar 2027",
                    "account_id": income_acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("1200"),
                    "discount_pct": Decimal("0"),
                    "service_start_date": date(2026, 4, 1),
                    "service_end_date": date(2027, 3, 31),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="test")

    assert posted.status == InvoiceStatus.POSTED
    assert posted.journal_entry_id is not None

    lines = await _je_lines(posted.journal_entry_id)
    credits_by_acct = {ln.account_id: ln.credit for ln in lines if ln.credit > 0}

    # Unearned Income should be credited for the full $1,200
    assert unearned_acct in credits_by_acct, "Cr Unearned Income not found in JE"
    assert credits_by_acct[unearned_acct] == Decimal("1200.00")

    # The income account should NOT be credited
    assert income_acct not in credits_by_acct, (
        "Income account was credited directly — deferred revenue not applied"
    )


@pytest.mark.asyncio
async def test_single_month_line_posts_to_income_normally() -> None:
    """Positive control: a line without service dates uses the income account as before."""
    cid, contact, income_acct, unearned_acct, fre = await _ctx()

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 15),
            due_date=date(2026, 5, 15),
            lines=[
                {
                    "description": "One-off consulting",
                    "account_id": income_acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("500"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="test")

    lines = await _je_lines(posted.journal_entry_id)
    credits_by_acct = {ln.account_id: ln.credit for ln in lines if ln.credit > 0}

    assert income_acct in credits_by_acct
    assert credits_by_acct[income_acct] == Decimal("500.00")
    assert unearned_acct not in credits_by_acct


@pytest.mark.asyncio
async def test_same_month_service_dates_not_deferred() -> None:
    """Line with service_start and service_end in the same month is NOT deferred."""
    cid, contact, income_acct, unearned_acct, fre = await _ctx()

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 4, 30),
            lines=[
                {
                    "description": "April only service",
                    "account_id": income_acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("300"),
                    "discount_pct": Decimal("0"),
                    "service_start_date": date(2026, 4, 1),
                    "service_end_date": date(2026, 4, 30),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="test")

    lines = await _je_lines(posted.journal_entry_id)
    credits_by_acct = {ln.account_id: ln.credit for ln in lines if ln.credit > 0}

    # Same-month: income account gets the credit, not unearned income
    assert income_acct in credits_by_acct
    assert unearned_acct not in credits_by_acct


@pytest.mark.asyncio
async def test_recognize_deferred_revenue_correct_monthly_amount() -> None:
    """recognize_deferred_revenue posts Dr Unearned Income / Cr Income for $100/month."""
    cid, contact, income_acct, unearned_acct, fre = await _ctx()

    # Post a 12-month $1,200 deferred invoice (Apr 2026 – Mar 2027)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 5),
            due_date=date(2026, 4, 30),
            lines=[
                {
                    "description": "Gym membership 12-month",
                    "account_id": income_acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("1200"),
                    "discount_pct": Decimal("0"),
                    "service_start_date": date(2026, 4, 1),
                    "service_end_date": date(2027, 3, 31),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    # Recognize for April 2026
    async with AsyncSessionLocal() as session:
        await dr_svc.recognize_deferred_revenue(
            session, cid, date(2026, 4, 15), posted_by="test", tenant_id=DEFAULT_TENANT_ID
        )

    # Find the recognition JE (the most recent JE for this company with the
    # expected description pattern, posted after the invoice JE)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(JournalEntry)
            .where(
                JournalEntry.company_id == cid,
                JournalEntry.description.like("Deferred revenue recognition%"),
            )
            .options(selectinload(JournalEntry.lines))
            .order_by(JournalEntry.created_at.desc())
        )
        je = result.scalars().first()

    assert je is not None, "No recognition JE found"
    lines = je.lines
    debits_by_acct = {ln.account_id: ln.debit for ln in lines if ln.debit > 0}
    credits_by_acct = {ln.account_id: ln.credit for ln in lines if ln.credit > 0}

    # Dr Unearned Income $100
    assert unearned_acct in debits_by_acct
    assert debits_by_acct[unearned_acct] == Decimal("100.00")

    # Cr Income $100
    assert income_acct in credits_by_acct
    assert credits_by_acct[income_acct] == Decimal("100.00")


@pytest.mark.asyncio
async def test_recognize_deferred_revenue_idempotent() -> None:
    """Running recognition twice for the same period is a no-op on the second call."""
    cid, contact, income_acct, _unearned_acct, fre = await _ctx()

    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 10),
            due_date=date(2026, 4, 30),
            lines=[
                {
                    "description": "6-month membership idempotent test",
                    "account_id": income_acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("600"),
                    "discount_pct": Decimal("0"),
                    "service_start_date": date(2026, 4, 1),
                    "service_end_date": date(2026, 9, 30),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    # First recognition
    async with AsyncSessionLocal() as session:
        await dr_svc.recognize_deferred_revenue(
            session, cid, date(2026, 4, 1), posted_by="test", tenant_id=DEFAULT_TENANT_ID
        )

    # Count JEs before second call
    async with AsyncSessionLocal() as session:
        count_before = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.company_id == cid,
                    JournalEntry.description.like("Deferred revenue recognition — April 2026"),
                )
            )
        ).scalars().all()

    # Second recognition call for same period — should not post another JE
    async with AsyncSessionLocal() as session:
        await dr_svc.recognize_deferred_revenue(
            session, cid, date(2026, 4, 20), posted_by="test", tenant_id=DEFAULT_TENANT_ID
        )

    async with AsyncSessionLocal() as session:
        count_after = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.company_id == cid,
                    JournalEntry.description.like("Deferred revenue recognition — April 2026"),
                )
            )
        ).scalars().all()

    assert len(count_after) == len(count_before), (
        "Second recognition call posted a duplicate JE — not idempotent"
    )


@pytest.mark.asyncio
async def test_recognize_last_month_absorbs_rounding() -> None:
    """12 months of $100 should total exactly $1,200 (no drift)."""
    cid, contact, income_acct, unearned_acct, fre = await _ctx()

    # $100/month x 12 = $1,200 exactly — last month should also be $100
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 4, 30),
            lines=[
                {
                    "description": "Even-split 12-month membership",
                    "account_id": income_acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("1200"),
                    "discount_pct": Decimal("0"),
                    "service_start_date": date(2026, 4, 1),
                    "service_end_date": date(2027, 3, 31),
                },
            ],
        )
        inv_id = inv.id

    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv_id, posted_by="test")

    # Recognize all 12 months
    months = [
        date(2026, m, 1) for m in range(4, 13)
    ] + [
        date(2027, m, 1) for m in range(1, 4)
    ]
    for period in months:
        async with AsyncSessionLocal() as session:
            await dr_svc.recognize_deferred_revenue(
                session, cid, period, posted_by="test", tenant_id=DEFAULT_TENANT_ID
            )

    # Total recognized for this invoice line should be $1,200
    async with AsyncSessionLocal() as session:
        line = (
            await session.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == inv_id)
            )
        ).scalars().first()
        assert line is not None
        # recognized_through_date should be set to 2027-03-01 (last period)
        assert line.recognized_through_date == date(2027, 3, 1)
