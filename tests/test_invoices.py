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
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as svc

pytestmark = pytest.mark.postgres_only


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


# ---------------------------------------------------------------------------
# CIVL-2: retention_pct on invoice lines
# ---------------------------------------------------------------------------


async def _ensure_retentions_receivable(company_id: uuid.UUID) -> uuid.UUID:
    """Create Retentions Receivable (1-1220) if the seed hasn't added it yet."""
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "1-1220",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        acct = Account(
            company_id=company_id,
            code="1-1220",
            name="Retentions Receivable",
            account_type=AccountType.ASSET,
            reconcile=True,
            is_header=False,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
        return acct.id


@pytest.mark.asyncio
async def test_retention_pct_stored_on_line() -> None:
    """retention_pct is persisted on invoice lines and invoice totals are unaffected."""
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 28),
            due_date=date(2026, 5, 28),
            lines=[
                {
                    "description": "Civil progress claim #1",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("440000"),
                    "discount_pct": Decimal("0"),
                    "retention_pct": Decimal("5"),
                },
            ],
        )
    # Totals reflect the full claim (not net of retention); retention is a
    # payment withholding, not a price reduction or GST reduction.
    assert inv.subtotal == Decimal("440000.00")
    assert inv.tax_total == Decimal("44000.00")
    assert inv.total == Decimal("484000.00")
    assert inv.lines[0].retention_pct == Decimal("5.00")


@pytest.mark.asyncio
async def test_retention_pct_splits_ar_on_post() -> None:
    """Posting an invoice with retention splits Dr AR into Trade Debtors + Retentions Receivable.

    Scenario: $440k progress claim, 5% retention, 10% GST.
    Expected GL (base currency AUD):
      Dr Trade Debtors        462,000   (net payable: 418k + 44k GST)
      Dr Retentions Receivable 22,000   (5% of 440k, withheld until PC)
        Cr Income             440,000   (full revenue recognised)
        Cr GST Collected       44,000   (auto-posted: full GST on claim)
    """
    cid, contact, acct, gst, _fre = await _ctx()
    ret_acct_id = await _ensure_retentions_receivable(cid)

    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 28),
            due_date=date(2026, 5, 28),
            lines=[
                {
                    "description": "Civil progress claim #2",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("440000"),
                    "discount_pct": Decimal("0"),
                    "retention_pct": Decimal("5"),
                },
            ],
        )
        posted = await svc.post_invoice(session, inv.id)

    assert posted.status == InvoiceStatus.POSTED
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        from sqlalchemy.orm import selectinload
        je = (
            await session.execute(
                select(JournalEntry)
                .options(selectinload(JournalEntry.lines))
                .where(JournalEntry.id == posted.journal_entry_id)
            )
        ).scalar_one()
        lines = je.lines

    # Build a lookup: account_id → (total_debit, total_credit)
    by_acct: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for jl in lines:
        d, c = by_acct.get(jl.account_id, (Decimal("0"), Decimal("0")))
        by_acct[jl.account_id] = (d + jl.debit, c + jl.credit)

    # Retentions Receivable: Dr 22,000
    assert ret_acct_id in by_acct, "Retentions Receivable not debited"
    ret_dr, ret_cr = by_acct[ret_acct_id]
    assert ret_dr == Decimal("22000.00"), f"Expected Dr 22000, got {ret_dr}"
    assert ret_cr == Decimal("0.00")

    # AR (Trade Debtors 1-1200): Dr 462,000
    async with AsyncSessionLocal() as session:
        ar_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == cid,
                    Account.code == "1-1200",
                )
            )
        ).scalar_one()
    ar_dr, ar_cr = by_acct[ar_acct.id]
    assert ar_dr == Decimal("462000.00"), f"Expected Dr AR 462000, got {ar_dr}"
    assert ar_cr == Decimal("0.00")

    # Total debits equal total invoice amount (484,000)
    total_dr = sum(d for d, _c in by_acct.values())
    assert total_dr == Decimal("484000.00")


@pytest.mark.asyncio
async def test_no_retention_uses_standard_ar_path() -> None:
    """Positive control: zero retention_pct uses single Dr Trade Debtors (no regression)."""
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 28),
            due_date=date(2026, 5, 28),
            lines=[
                {
                    "description": "Standard invoice no retention",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100000"),
                    "discount_pct": Decimal("0"),
                    # retention_pct omitted — defaults to 0
                },
            ],
        )
        posted = await svc.post_invoice(session, inv.id)

    assert posted.status == InvoiceStatus.POSTED
    async with AsyncSessionLocal() as session:
        from sqlalchemy.orm import selectinload
        je = (
            await session.execute(
                select(JournalEntry)
                .options(selectinload(JournalEntry.lines))
                .where(JournalEntry.id == posted.journal_entry_id)
            )
        ).scalar_one()
        lines = je.lines

    debit_lines = [jl for jl in lines if jl.debit > Decimal("0")]
    # Only one Dr line — Trade Debtors for full 110,000
    assert len(debit_lines) == 1
    assert debit_lines[0].debit == Decimal("110000.00")
    assert inv.lines[0].margin_acq_cost is None


# ---------------------------------------------------------------------------
# RLES-6: settlement_date drives BAS period attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_date_used_as_journal_entry_date() -> None:
    """GL entry_date uses settlement_date, not issue_date, when set (RLES-6).

    Real estate commissions are earned at unconditional exchange/settlement.
    BAS period must follow settlement_date so the monthly allocation is correct.
    """
    cid, contact, acct, gst, _fre = await _ctx()
    issue = date(2026, 5, 1)
    settlement = date(2026, 6, 28)  # ~8 weeks later — different BAS period

    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=issue,
            due_date=issue + timedelta(days=30),
            settlement_date=settlement,
            lines=[
                {
                    "description": "Commission — 123 Main St",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": "1",
                    "unit_price": "11000",
                    "discount_pct": "0",
                }
            ],
        )
    assert inv.settlement_date == settlement

    async with AsyncSessionLocal() as session:
        posted = await svc.post_invoice(session, inv.id, posted_by="test")

    assert posted.journal_entry_id is not None
    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        # GL date must be settlement, not issue — this is the BAS attribution date
        assert je.entry_date == settlement
        assert je.entry_date != issue


@pytest.mark.asyncio
async def test_no_settlement_date_falls_back_to_issue_date() -> None:
    """Without settlement_date the GL entry_date is issue_date (existing behaviour)."""
    cid, contact, acct, gst, _fre = await _ctx()
    issue = date(2026, 5, 15)

    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=issue,
            due_date=issue + timedelta(days=30),
            lines=[
                {
                    "description": "Commission — no settlement date",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": "1",
                    "unit_price": "5500",
                    "discount_pct": "0",
                }
            ],
        )
    assert inv.settlement_date is None

    async with AsyncSessionLocal() as session:
        posted = await svc.post_invoice(session, inv.id, posted_by="test")

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        assert je.entry_date == issue


# ---------------------------------------------------------------------------
# MOTR-2: trade-in vehicle recorded as separate AP bill, not negative line
# ---------------------------------------------------------------------------


async def _ensure_inventory_account(company_id: uuid.UUID) -> uuid.UUID:
    """Return (or create) a test asset account for trade-in vehicle inventory."""
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "1-1350",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        acct = Account(
            company_id=company_id,
            code="1-1350",
            name="Used Vehicle Stock",
            account_type=AccountType.ASSET,
            is_header=False,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
        return acct.id


@pytest.mark.asyncio
async def test_trade_in_excluded_from_invoice_totals() -> None:
    """Trade-in line (is_trade_in=True) is excluded from the invoice G1 total.

    New-car sale $65,000 + GST $6,500 → invoice total $71,500.
    Trade-in $15,000 (is_trade_in=True) does NOT reduce that total.
    """
    cid, contact, income_acct, gst, _fre = await _ctx()
    inv_acct = await _ensure_inventory_account(cid)

    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 1),
            due_date=date(2026, 5, 31),
            lines=[
                {
                    "description": "New vehicle sale",
                    "account_id": income_acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("65000"),
                    "discount_pct": Decimal("0"),
                },
                {
                    "description": "Trade-in: 2019 Toyota Camry",
                    "account_id": inv_acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("15000"),
                    "discount_pct": Decimal("0"),
                    "is_trade_in": True,
                },
            ],
        )

    # Invoice header totals reflect only the new-car sale, not the trade-in
    assert inv.subtotal == Decimal("65000.00")
    assert inv.tax_total == Decimal("6500.00")
    assert inv.total == Decimal("71500.00")

    # Both lines are stored; trade-in flag is persisted
    assert len(inv.lines) == 2
    trade_in_line = next(ln for ln in inv.lines if ln.is_trade_in)
    assert trade_in_line.line_subtotal == Decimal("15000.00")
    assert trade_in_line.line_tax == Decimal("1500.00")


@pytest.mark.asyncio
async def test_trade_in_post_creates_companion_bill() -> None:
    """Posting an invoice with a trade-in line auto-creates a companion AP bill.

    Invoice (G1 sale):   Dr AR 71,500 / Cr Income 65,000 / Cr GST Collected 6,500
    Companion bill (AP): Dr Vehicle Inventory 15,000 / Dr GST Paid 1,500 / Cr AP 16,500
    Net cash settlement: $71,500 AR − $16,500 AP = $55,000  (but each leg is separate)
    """
    from saebooks.models.bill import Bill, BillStatus

    cid, contact, income_acct, gst, _fre = await _ctx()
    inv_acct = await _ensure_inventory_account(cid)

    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 2),
            due_date=date(2026, 6, 2),
            lines=[
                {
                    "description": "New vehicle — VIN XYZ999",
                    "account_id": income_acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("65000"),
                    "discount_pct": Decimal("0"),
                },
                {
                    "description": "Trade-in: 2020 Mazda CX-5",
                    "account_id": inv_acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("15000"),
                    "discount_pct": Decimal("0"),
                    "is_trade_in": True,
                },
            ],
        )
        posted = await svc.post_invoice(session, inv.id, posted_by="test")

    # Invoice GL — only the new-car sale; trade-in is NOT a line here
    assert posted.status == InvoiceStatus.POSTED
    assert posted.total == Decimal("71500.00")

    async with AsyncSessionLocal() as session:
        from sqlalchemy.orm import selectinload

        je = (
            await session.execute(
                select(JournalEntry)
                .options(selectinload(JournalEntry.lines))
                .where(JournalEntry.id == posted.journal_entry_id)
            )
        ).scalar_one()
        inv_je_debit_total = sum(jl.debit for jl in je.lines)
        inv_je_credit_total = sum(jl.credit for jl in je.lines)

    # Invoice journal must balance and Dr AR = 71,500
    assert inv_je_debit_total == inv_je_credit_total
    assert inv_je_debit_total == Decimal("71500.00")

    # A companion bill must exist for the trade-in, posted, with correct total
    async with AsyncSessionLocal() as session:
        bills = (
            await session.execute(
                select(Bill)
                .where(
                    Bill.company_id == cid,
                    Bill.contact_id == contact,
                    Bill.status == BillStatus.POSTED,
                    Bill.supplier_reference == posted.number,
                )
            )
        ).scalars().all()

    assert len(bills) == 1, "Expected exactly one trade-in bill to be auto-created"
    bill = bills[0]
    assert bill.total == Decimal("16500.00"), (
        f"Trade-in bill total should be 15,000 + 10% GST = 16,500 (got {bill.total})"
    )


@pytest.mark.asyncio
async def test_trade_in_negative_unit_price_rejected() -> None:
    """Negative unit_price on a trade-in line is rejected at create time."""
    cid, contact, income_acct, gst, _fre = await _ctx()
    inv_acct = await _ensure_inventory_account(cid)

    with pytest.raises(svc.InvoiceError, match="positive value"):
        async with AsyncSessionLocal() as session:
            await svc.create_draft(
                session,
                company_id=cid,
                contact_id=contact,
                issue_date=date(2026, 5, 3),
                due_date=date(2026, 6, 3),
                lines=[
                    {
                        "description": "New vehicle",
                        "account_id": income_acct,
                        "tax_code_id": gst,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("65000"),
                        "discount_pct": Decimal("0"),
                    },
                    {
                        "description": "Trade-in (wrong: negative price)",
                        "account_id": inv_acct,
                        "tax_code_id": gst,
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("-15000"),
                        "discount_pct": Decimal("0"),
                        "is_trade_in": True,
                    },
                ],
            )
