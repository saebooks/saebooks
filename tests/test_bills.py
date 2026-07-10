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
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as svc

pytestmark = pytest.mark.postgres_only


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
        # JE-provenance: a bill-posted JE self-declares its origin + source.
        assert je.origin == JournalOrigin.BILL
        assert je.source_type == "bill"
        assert je.source_id == posted.id
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


# ---------------------------------------------------------------------------
# CIVL-3: retention_pct on bill lines
# ---------------------------------------------------------------------------


async def _ensure_retentions_payable(company_id: uuid.UUID) -> uuid.UUID:
    """Create Retentions Payable (2-1850) if the seed hasn't added it yet."""
    async with AsyncSessionLocal() as session:
        from saebooks.models.account import AccountType as _AT
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "2-1850",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        acct = Account(
            company_id=company_id,
            code="2-1850",
            name="Retentions Payable",
            account_type=_AT.LIABILITY,
            is_header=False,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
        return acct.id


@pytest.mark.asyncio
async def test_retention_pct_stored_on_bill_line() -> None:
    """retention_pct is persisted on bill lines; totals reflect the full invoice."""
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 1),
            due_date=date(2026, 6, 1),
            lines=[
                {
                    "description": "Sub-contractor works",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("66000"),
                    "discount_pct": Decimal("0"),
                    "retention_pct": Decimal("5"),
                },
            ],
        )
    # Totals reflect the full invoice; retention is a payment deferral,
    # not a price reduction or GST reduction.
    assert bill.subtotal == Decimal("66000.00")
    assert bill.tax_total == Decimal("6600.00")
    assert bill.total == Decimal("72600.00")
    assert bill.lines[0].retention_pct == Decimal("5.00")


@pytest.mark.asyncio
async def test_retention_pct_splits_ap_on_post() -> None:
    """Posting a bill with retention splits Cr AP into Trade Creditors + Retentions Payable.

    Scenario: $66k sub-contractor bill, 5% retention, 10% GST.
    Expected GL (base currency AUD):
      Dr Expense               66,000   (full ex-GST cost recognised)
      Dr GST Paid               6,600   (auto-posted: full GST input credit)
        Cr Trade Creditors     69,300   (net payable: 62.7k + 6.6k GST)
        Cr Retentions Payable   3,300   (5% of 66k, held until PC)
    """
    cid, contact, acct, gst, _fre = await _ctx()
    await _ensure_retentions_payable(cid)

    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 2),
            due_date=date(2026, 6, 2),
            lines=[
                {
                    "description": "Sub-contractor works",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("66000"),
                    "discount_pct": Decimal("0"),
                    "retention_pct": Decimal("5"),
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_bill(session, bill.id, posted_by="test")

    assert posted.status == BillStatus.POSTED
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        lines_result = await session.execute(
            select(JournalLine).where(JournalLine.entry_id == je.id)
        )
        jlines = lines_result.scalars().all()

    # Collect totals by account code.
    acct_credits: dict[str, Decimal] = {}
    total_debit = Decimal("0")
    for jl in jlines:
        async with AsyncSessionLocal() as s2:
            a = await s2.get(Account, jl.account_id)
        assert a is not None
        if jl.credit > Decimal("0"):
            acct_credits[a.code] = acct_credits.get(a.code, Decimal("0")) + jl.credit
        if jl.debit > Decimal("0"):
            total_debit += jl.debit

    # Expense Dr 66,000 + GST Paid Dr 6,600 = 72,600 total debit
    assert total_debit == Decimal("72600.00")
    # Trade Creditors Cr: 72,600 - 3,300 = 69,300
    assert acct_credits.get("2-1200") == Decimal("69300.00")
    # Retentions Payable Cr: 5% of 66,000 = 3,300
    assert acct_credits.get("2-1850") == Decimal("3300.00")


@pytest.mark.asyncio
async def test_no_retention_uses_standard_ap_path() -> None:
    """Positive control: zero retention_pct uses single Cr Trade Creditors (no regression)."""
    cid, contact, acct, gst, _fre = await _ctx()
    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 3),
            due_date=date(2026, 6, 3),
            lines=[
                {
                    "description": "Standard bill no retention",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("50000"),
                    "discount_pct": Decimal("0"),
                    # retention_pct omitted — defaults to 0
                },
            ],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_bill(session, bill.id, posted_by="test")

    assert posted.status == BillStatus.POSTED

    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, posted.journal_entry_id)
        assert je is not None
        lines_result = await session.execute(
            select(JournalLine).where(JournalLine.entry_id == je.id)
        )
        jlines = lines_result.scalars().all()

    ap_credits = Decimal("0")
    retention_credits = Decimal("0")
    for jl in jlines:
        async with AsyncSessionLocal() as s2:
            a = await s2.get(Account, jl.account_id)
        assert a is not None
        if a.code == "2-1200":
            ap_credits += jl.credit
        if a.code == "2-1850":
            retention_credits += jl.credit

    # Standard path: full amount to Trade Creditors, nothing to Retentions Payable.
    assert ap_credits == Decimal("55000.00")  # 50k + 5k GST
    assert retention_credits == Decimal("0.00")


@pytest.mark.asyncio
async def test_post_bill_rejects_reverse_charge_eu_acquisition() -> None:
    """Critic-round-4 fix: post_bill must refuse an EU-acquisition
    reverse-charge tax code (rc_eu_acq_goods/services) rather than
    silently overstate Accounts Payable by the self-assessed VAT and
    never book the output-side liability — see
    ``services.bills._reject_unsupported_reverse_charge``."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        company = Company(
            id=company_id,
            name=f"RC Guard Test {company_id.hex[:8]}",
            base_currency="EUR",
            fin_year_start_month=1,
            audit_mode="immutable",
            jurisdiction="EE",
        )
        session.add(company)
        await session.flush()

        expense_acct = Account(
            company_id=company_id,
            code="6-1000",
            name="Purchases",
            account_type=AccountType.EXPENSE,
        )
        ap_acct = Account(
            company_id=company_id,
            code="2-1200",
            name="Trade Creditors",
            account_type=AccountType.LIABILITY,
        )
        session.add_all([expense_acct, ap_acct])
        await session.flush()

        rc_tc = TaxCode(
            company_id=company_id,
            code="RC-EUACQ",
            name="EE reverse charge — EU acquisition of goods (24%)",
            rate=Decimal("24.000"),
            tax_system="VAT",
            jurisdiction="EE",
            reporting_type="rc_eu_acq_goods",
        )
        session.add(rc_tc)

        contact = Contact(
            company_id=company_id,
            name="EU Supplier OU",
            contact_type=ContactType.SUPPLIER,
            email="eu-supplier@example.com",
        )
        session.add(contact)
        await session.commit()
        await session.refresh(rc_tc)
        await session.refresh(contact)

    async with AsyncSessionLocal() as session:
        bill = await svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact.id,
            issue_date=date(2026, 6, 1),
            due_date=date(2026, 6, 30),
            lines=[
                {
                    "description": "EU acquisition of goods",
                    "account_id": expense_acct.id,
                    "tax_code_id": rc_tc.id,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("4000"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.BillError, match="reverse-charge"):
            await svc.post_bill(session, bill.id, posted_by="test")

    # Confirm no journal was posted — the bill is still a DRAFT.
    async with AsyncSessionLocal() as session:
        refreshed = await session.get(Bill, bill.id)
        assert refreshed is not None
        assert refreshed.status == BillStatus.DRAFT
        assert refreshed.journal_entry_id is None
