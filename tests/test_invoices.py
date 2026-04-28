"""Tests for ``saebooks.services.invoices``.

Covers the full AR invoice lifecycle:

1. ``create_draft`` computes line totals correctly with add-on GST.
2. ``update_draft`` replaces lines and recalcs.
3. ``post_invoice`` mints the number via numbering, posts the GL
   journal (AR debit + income credit + auto-GST credit), stamps the
   invoice row.
4. ``void_invoice`` reverses the journal and marks VOIDED.
5. Tax-free lines produce zero line_tax.
6. Discount percent applies correctly.
7. Posting an empty invoice raises InvoiceError.
8. Posting a second time raises InvoiceError.
9. Void of an unposted DRAFT flips to VOIDED without touching GL.
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
from saebooks.services import invoices as svc


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_acct_id, gst_tax_code_id, free_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
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
        fre_tc = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "FRE",
                )
            )
        ).scalar_one()

        # Ensure a test contact exists.
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test Invoices Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test Invoices Ltd",
                contact_type=ContactType.CUSTOMER,
                email="acme@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        return company.id, contact.id, income_acct.id, gst_tc.id, fre_tc.id


@pytest.mark.asyncio
async def test_create_draft_computes_line_totals() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
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
                    "quantity": Decimal("10"),
                    "unit_price": Decimal("150"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    assert inv.status == InvoiceStatus.DRAFT
    assert inv.number is None
    assert inv.subtotal == Decimal("1500.00")
    assert inv.tax_total == Decimal("150.00")
    assert inv.total == Decimal("1650.00")
    assert len(inv.lines) == 1
    assert inv.lines[0].line_total == Decimal("1650.00")


@pytest.mark.asyncio
async def test_tax_free_line_has_zero_tax() -> None:
    cid, contact, acct, _gst, fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {
                    "description": "GST-free food",
                    "account_id": acct,
                    "tax_code_id": fre,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    assert inv.tax_total == Decimal("0.00")
    assert inv.total == Decimal("100.00")


@pytest.mark.asyncio
async def test_discount_applies() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {
                    "description": "Discounted widget",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("2"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("10"),
                }
            ],
        )
    # 2 * 100 * 0.9 = 180, +10% = 198
    assert inv.subtotal == Decimal("180.00")
    assert inv.tax_total == Decimal("18.00")
    assert inv.total == Decimal("198.00")


@pytest.mark.asyncio
async def test_update_draft_replaces_lines() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {"description": "A", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "100", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        inv2 = await svc.update_draft(
            session, inv.id,
            lines=[
                {"description": "B", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "200", "discount_pct": "0"},
                {"description": "C", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "50",  "discount_pct": "0"},
            ],
        )
    assert len(inv2.lines) == 2
    assert inv2.subtotal == Decimal("250.00")
    assert inv2.total == Decimal("275.00")


@pytest.mark.asyncio
async def test_post_invoice_mints_number_and_journal() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {"description": "Services", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "1000", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_invoice(session, inv.id, posted_by="test")
    assert posted.status == InvoiceStatus.POSTED
    assert posted.number is not None
    assert posted.number.startswith("INV-")
    assert posted.journal_entry_id is not None

    # Journal entry balances and includes GST auto-post
    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        assert je.status == EntryStatus.POSTED
        await session.refresh(je, ["lines"])
        total_dr = sum((ln.debit for ln in je.lines), Decimal("0"))
        total_cr = sum((ln.credit for ln in je.lines), Decimal("0"))
        assert total_dr == total_cr
        assert total_dr == Decimal("1100.00")  # DR AR 1100
        # At least 3 lines: AR, income, GST collected
        assert len(je.lines) >= 3


@pytest.mark.asyncio
async def test_void_posted_invoice_reverses_journal() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
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
        await svc.post_invoice(session, inv.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        voided = await svc.void_invoice(session, inv.id, posted_by="test")
    assert voided.status == InvoiceStatus.VOIDED
    assert voided.void_journal_entry_id is not None


@pytest.mark.asyncio
async def test_void_draft_flips_status_without_journal() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {"description": "Draft-only", "account_id": acct, "tax_code_id": gst,
                 "quantity": "1", "unit_price": "50", "discount_pct": "0"},
            ],
        )
    async with AsyncSessionLocal() as session:
        voided = await svc.void_invoice(session, inv.id)
    assert voided.status == InvoiceStatus.VOIDED
    assert voided.void_journal_entry_id is None


@pytest.mark.asyncio
async def test_cannot_post_empty_invoice() -> None:
    cid, contact, _acct, _gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[],
        )
    with pytest.raises(svc.InvoiceError, match="no lines"):
        async with AsyncSessionLocal() as session:
            await svc.post_invoice(session, inv.id)


@pytest.mark.asyncio
async def test_cannot_post_twice() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
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
        await svc.post_invoice(session, inv.id)
    with pytest.raises(svc.InvoiceError, match="already posted"):
        async with AsyncSessionLocal() as session:
            await svc.post_invoice(session, inv.id)


@pytest.mark.asyncio
async def test_cannot_edit_posted() -> None:
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
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
        await svc.post_invoice(session, inv.id)
    with pytest.raises(svc.InvoiceError, match="Cannot edit"):
        async with AsyncSessionLocal() as session:
            await svc.update_draft(session, inv.id, notes="nope")


@pytest.mark.asyncio
async def test_margin_scheme_gst_div75() -> None:
    """Margin-scheme (MGN) lines compute GST as 1/11 × (sale − acq_cost).

    ATO example: vehicle bought $22,000, sold $24,000.
    Margin = $2,000. GST = $2,000 / 11 = $181.82 (rounded half-up).
    Standard 10 % on $24,000 would wrongly give $2,400.
    """
    cid, contact, acct, _gst, _fre = await _ctx()

    async with AsyncSessionLocal() as session:
        mgn_tc = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == cid,
                    TaxCode.code == "MGN",
                )
            )
        ).scalar_one()

        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {
                    "description": "Used vehicle VIN ABC123",
                    "account_id": acct,
                    "tax_code_id": mgn_tc.id,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("24000"),
                    "discount_pct": Decimal("0"),
                    "margin_acq_cost": Decimal("22000"),
                },
            ],
        )

    # subtotal = $24,000; margin = $24,000 − $22,000 = $2,000
    # GST = $2,000 / 11 = $181.818... → $181.82
    assert inv.subtotal == Decimal("24000.00")
    assert inv.tax_total == Decimal("181.82")
    assert inv.total == Decimal("24181.82")
    assert inv.lines[0].margin_acq_cost == Decimal("22000.00")


@pytest.mark.asyncio
async def test_margin_scheme_zero_acq_cost_treated_as_full_margin() -> None:
    """When margin_acq_cost is omitted, margin equals the full sale price."""
    cid, contact, acct, _gst, _fre = await _ctx()

    async with AsyncSessionLocal() as session:
        mgn_tc = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == cid,
                    TaxCode.code == "MGN",
                )
            )
        ).scalar_one()

        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            due_date=date(2026, 5, 20),
            lines=[
                {
                    "description": "Vehicle (no acq cost entered)",
                    "account_id": acct,
                    "tax_code_id": mgn_tc.id,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("11000"),
                    "discount_pct": Decimal("0"),
                    # margin_acq_cost intentionally omitted
                },
            ],
        )

    # margin = $11,000 − $0 = $11,000; GST = $11,000 / 11 = $1,000.00
    assert inv.tax_total == Decimal("1000.00")
    assert inv.lines[0].margin_acq_cost is None
