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


@pytest.mark.asyncio
async def test_post_invoice_writes_normalised_tax_components() -> None:
    """M1.5 · T2 — posting also writes normalised journal_line_tax_component
    rows alongside the JSONB snapshot, so co-existing taxes are queryable.

    Mirrors the tax_treatment snapshot: a 10% GST invoice's output line
    yields one component row, family vat_gst, tax 100.00.
    """
    from saebooks.models.journal import JournalLine
    from saebooks.models.journal_line_tax_component import (
        JournalLineTaxComponent,
    )

    cid, contact, acct, gst = await _ctx()
    today = date(2026, 5, 11)
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
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="t2-test")
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        line_ids = (
            await session.execute(
                select(JournalLine.id).where(
                    JournalLine.entry_id == posted.journal_entry_id
                )
            )
        ).scalars().all()
        components = (
            await session.execute(
                select(JournalLineTaxComponent).where(
                    JournalLineTaxComponent.journal_line_id.in_(line_ids)
                )
            )
        ).scalars().all()

        # At least the income line produced a component; every component is
        # a known canonical family and ties to its line.
        assert components, "expected normalised tax components on the posted entry"
        for c in components:
            assert c.tax_family in {"vat_gst", "us_sales_use", "excise",
                                    "customs_duty", "withholding", "other"}
            assert c.company_id == cid
            assert c.direction in {"output", "input", "none"}

        output = [c for c in components if c.direction == "output"]
        assert output, "expected an output-direction GST component"
        gst_comp = output[0]
        assert gst_comp.tax_family == "vat_gst"
        assert gst_comp.ref_tax_code == "GST"
        assert gst_comp.base_amount == Decimal("1000.00")
        assert gst_comp.tax_amount == Decimal("100.00")


@pytest.mark.asyncio
async def test_voiding_taxable_invoice_mirrors_offsetting_reversal_components() -> None:
    """Finding 3 (supersedes the earlier "no reversal components" rule):
    voiding a taxable invoice MIRRORS the original's tax components onto
    the reversal lines so a component-aware BAS reader nets them.

    Both the REVERSED original and the POSTED reversal are in
    REPORTABLE_STATUSES, so the reversal must carry an offsetting tax
    component or the tax boxes stay overstated after a void (the base
    already nets via the swapped debit/credit). The aggregator signs a
    reversal entry's contribution negative, so the mirrored component
    (same positive tax as the original) cancels — it does NOT double-count.
    Asserts the reversal carries a mirrored component equal to the
    original's tax.
    """
    from saebooks.models.journal import JournalEntry, JournalLine
    from saebooks.models.journal_line_tax_component import (
        JournalLineTaxComponent,
    )

    cid, contact, acct, gst = await _ctx()
    today = date(2026, 6, 15)
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
        posted = await inv_svc.post_invoice(session, inv.id, posted_by="t2-rev")
    orig_entry = posted.journal_entry_id
    assert orig_entry is not None

    async with AsyncSessionLocal() as session:
        await inv_svc.void_invoice(session, inv.id, posted_by="t2-rev-void")

    async with AsyncSessionLocal() as session:
        # The reversal entry is the one whose reversal_of_id points at the
        # original posted entry.
        reversal_ids = (
            await session.execute(
                select(JournalEntry.id).where(
                    JournalEntry.reversal_of_id == orig_entry
                )
            )
        ).scalars().all()
        assert reversal_ids, "expected a reversal entry after void"

        rev_line_ids = (
            await session.execute(
                select(JournalLine.id).where(
                    JournalLine.entry_id.in_(reversal_ids)
                )
            )
        ).scalars().all()
        rev_components = (
            await session.execute(
                select(JournalLineTaxComponent).where(
                    JournalLineTaxComponent.journal_line_id.in_(rev_line_ids)
                )
            )
        ).scalars().all()
        assert rev_components, (
            "reversal must mirror the original's tax component(s) so a "
            "component-aware reader nets the void — got none"
        )
        # €1000 @ 10% GST → one output component, tax 100.00, mirrored
        # with the SAME positive sign (the aggregator negates the whole
        # reversal entry, so this nets rather than double-counting).
        assert [str(c.tax_amount) for c in rev_components] == ["100.00"]
        assert all(c.direction == "output" for c in rev_components)
