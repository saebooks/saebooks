"""Verify ``journal_lines.tax_treatment`` is populated by ``post()``.

Sub-task 4 of the M0 multi-jurisdiction refactor: every JE posted via
``services.journal.post`` (and by extension every invoice/bill that
ends up there) should carry a per-line tax-determination snapshot in
the JSONB ``tax_treatment`` column, produced by the company's
``TaxEngine.compute``.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as inv_svc
pytestmark = pytest.mark.postgres_only


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

        income_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "4-6000",
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

        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Tax Treatment Test Co",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Tax Treatment Test Co",
                contact_type=ContactType.CUSTOMER,
                email="tt-test@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, income_acct.id, gst_tc.id


@pytest.mark.asyncio
async def test_post_invoice_populates_tax_treatment_on_lines() -> None:
    """Posting a 10% GST invoice writes a TaxTreatment snapshot on every line."""
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Consulting",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": "1",
                    "unit_price": "1000",
                    "discount_pct": "0",
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="tt-test")
    assert posted.status == InvoiceStatus.POSTED
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        assert je.status == EntryStatus.POSTED
        await session.refresh(je, ["lines"])

        # Every line must carry a tax_treatment snapshot.
        for line in je.lines:
            assert line.tax_treatment is not None, (
                f"line {line.line_no} (account {line.account_id}) missing tax_treatment"
            )
            tt = line.tax_treatment
            assert tt["jurisdiction"] == "AU"
            assert tt["code"] in {"GST", "FRE", "EXP"}
            # Decimal-as-string round trip (no float drift).
            Decimal(tt["rate"])
            Decimal(tt["base"])
            Decimal(tt["tax"])
            assert tt["direction"] in {"output", "input", "none"}
            assert "reporting_type" in tt

        # The income line specifically should be 'output' direction with
        # rate 0.10 and tax 100.00 (10% of 1000).
        income_lines = [
            ln for ln in je.lines
            if ln.tax_treatment and ln.tax_treatment["direction"] == "output"
        ]
        assert income_lines, "expected at least one output-direction line"
        income_line = income_lines[0]
        assert income_line.tax_treatment["jurisdiction"] == "AU"
        assert income_line.tax_treatment["code"] == "GST"
        # TaxCode.rate is stored as percentage points (10.000 == 10%).
        # The engine round-trips the value as-is — fraction conversion
        # is up to consumers.
        assert Decimal(income_line.tax_treatment["rate"]) == Decimal("10.000")
        # Base is the line amount (1000); tax is the gst_amount snapshotted
        # by the invoice service (100.00).
        assert Decimal(income_line.tax_treatment["base"]) == Decimal("1000.00")
        assert Decimal(income_line.tax_treatment["tax"]) == Decimal("100.00")
        assert income_line.tax_treatment["reporting_type"] == "taxable"


@pytest.mark.asyncio
async def test_post_invoice_populates_tax_treatment_on_gst_auto_lines() -> None:
    """The GST Collected line auto-posted by gst_svc also gets a treatment.

    The auto-poster runs before _apply_tax_treatment, so by the time the
    snapshot loop runs the GST line is already in entry.lines and gets
    its own TaxTreatment (direction='none' because GST liability accounts
    aren't in the input/output sets).
    """
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await inv_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Service+GST",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": "1",
                    "unit_price": "500",
                    "discount_pct": "0",
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="tt-test")

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        await session.refresh(je, ["lines"])

        # Every line — AR, income, GST collected — has a treatment.
        treatments_present = sum(1 for ln in je.lines if ln.tax_treatment is not None)
        assert treatments_present == len(je.lines)
