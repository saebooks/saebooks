"""Tests for ``saebooks.services.credit_notes``.

Covers:

1. ``create_draft`` computes line totals with GST.
2. ``post_credit_note`` mints number + posts reverse-sign journal
   (Dr Income + Dr GST Collected / Cr AR).
3. Empty / non-positive credit notes rejected.
4. Editing a posted credit note rejected.
5. ``void_credit_note`` on posted → reverse journal.
6. Draft void flips status without GL touch.
7. Contra-COGS posting (hold-back): Cr COGS / Cr GST / Dr AR (BAS G8).
8. Mixed INCOME + COGS lines rejected at posting.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.credit_note import CreditNote, CreditNoteStatus
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode
from saebooks.services import credit_notes as svc

pytestmark = pytest.mark.postgres_only


async def _fast_forward_credit_note_counter() -> None:
    """Advance the per-company credit-note DocumentCounter past any
    existing CN-NNNNNN number already in the DB — see ``test_bills.py``
    for the full rationale.
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
                select(CreditNote.number).where(
                    CreditNote.company_id == company.id,
                    CreditNote.number.isnot(None),
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
                    DocumentCounter.kind == "credit_note",
                )
            )
        ).scalar_one_or_none()
        if counter is None:
            counter = DocumentCounter(
                company_id=company.id,
                kind="credit_note",
                prefix="CN-",
                pad_width=6,
                next_value=max_suffix + 1,
            )
            session.add(counter)
        elif counter.next_value <= max_suffix:
            counter.next_value = max_suffix + 1
        await session.commit()


@pytest.fixture(autouse=True, scope="module")
async def _prep_credit_note_counter() -> AsyncGenerator[None, None]:
    await _fast_forward_credit_note_counter()
    yield


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_account_id, gst_tax_code_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
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
                    Contact.name == "Test CreditNotes Ltd",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id,
                name="Test CreditNotes Ltd",
                contact_type=ContactType.CUSTOMER,
                email="cn@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing
        return company.id, contact.id, income.id, gst.id


def _line(income: uuid.UUID, gst: uuid.UUID, amount: Decimal) -> dict[str, object]:
    return {
        "description": "Refund",
        "account_id": income,
        "tax_code_id": gst,
        "quantity": Decimal("1"),
        "unit_price": amount,
        "discount_pct": Decimal("0"),
    }


async def _ctx_with_cogs() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, contact_id, income_account_id, cogs_account_id, gst_tax_code_id)."""
    cid, contact, income, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cogs = (
            await session.execute(
                select(Account).where(
                    Account.company_id == cid,
                    Account.code == "5-2000",
                )
            )
        ).scalar_one()
    return cid, contact, income, cogs.id, gst


@pytest.mark.asyncio
async def test_create_draft_computes_totals_with_gst() -> None:
    cid, contact, income, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(income, gst, Decimal("100.00"))],
            reason="Return of defective goods",
        )
    assert cn.subtotal == Decimal("100.00")
    assert cn.tax_total == Decimal("10.00")  # 10% GST
    assert cn.total == Decimal("110.00")
    assert cn.status == CreditNoteStatus.DRAFT
    assert cn.number is None


@pytest.mark.asyncio
async def test_post_credit_note_mints_number_and_reverse_sign_journal() -> None:
    cid, contact, income, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(income, gst, Decimal("200.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_credit_note(session, cn.id, posted_by="test")

    assert posted.status == CreditNoteStatus.POSTED
    assert posted.number is not None
    assert posted.number.startswith("CN-")
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, posted.journal_entry_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        lines = (
            await session.execute(
                select(JournalLine)
                .where(JournalLine.entry_id == entry.id)
                .order_by(JournalLine.line_no)
            )
        ).scalars().all()

        # Verify reverse-sign: Income debited (mirror of invoice credit),
        # GST Collected debited (mirror), AR credited (mirror of invoice debit).
        debits = sum((ln.debit for ln in lines), Decimal("0"))
        credits = sum((ln.credit for ln in lines), Decimal("0"))
        assert debits == credits
        assert debits == Decimal("220.00")

        # Confirm the account codes.
        account_debits: dict[str, Decimal] = {}
        account_credits: dict[str, Decimal] = {}
        for ln in lines:
            acct = await session.get(Account, ln.account_id)
            assert acct is not None
            if ln.debit > 0:
                account_debits[acct.code] = (
                    account_debits.get(acct.code, Decimal("0")) + ln.debit
                )
            if ln.credit > 0:
                account_credits[acct.code] = (
                    account_credits.get(acct.code, Decimal("0")) + ln.credit
                )

        assert account_debits.get("4-6000") == Decimal("200.00")
        assert account_debits.get("2-1310") == Decimal("20.00")
        assert account_credits.get("1-1200") == Decimal("220.00")


@pytest.mark.asyncio
async def test_post_rejects_empty_credit_note() -> None:
    cid, contact, _i, _g = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
        )
    with pytest.raises(svc.CreditNoteError, match="no lines"):
        async with AsyncSessionLocal() as session:
            await svc.post_credit_note(session, cn.id, posted_by="test")


@pytest.mark.asyncio
async def test_cannot_edit_posted_credit_note() -> None:
    cid, contact, income, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(income, gst, Decimal("50.00"))],
        )
    async with AsyncSessionLocal() as session:
        await svc.post_credit_note(session, cn.id, posted_by="test")
    with pytest.raises(svc.CreditNoteError, match="Cannot edit"):
        async with AsyncSessionLocal() as session:
            await svc.update_draft(session, cn.id, reason="nope")


@pytest.mark.asyncio
async def test_void_posted_reverses_journal() -> None:
    cid, contact, income, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(income, gst, Decimal("75.00"))],
        )
    async with AsyncSessionLocal() as session:
        await svc.post_credit_note(session, cn.id, posted_by="test")
    async with AsyncSessionLocal() as session:
        voided = await svc.void_credit_note(session, cn.id, posted_by="test")
    assert voided.status == CreditNoteStatus.VOIDED
    assert voided.void_journal_entry_id is not None


@pytest.mark.asyncio
async def test_void_draft_flips_status_without_journal() -> None:
    cid, contact, income, gst = await _ctx()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 4, 20),
            lines=[_line(income, gst, Decimal("10.00"))],
        )
    async with AsyncSessionLocal() as session:
        voided = await svc.void_credit_note(session, cn.id)
    assert voided.status == CreditNoteStatus.VOIDED
    assert voided.void_journal_entry_id is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_prep_credit_note_counter")
async def test_post_cogs_credit_note_contra_cogs_journal() -> None:
    """Hold-back / rebate posted to COGS account: Cr COGS / Cr GST / Dr AR (BAS G8)."""
    cid, contact, _income, cogs, gst = await _ctx_with_cogs()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 1),
            lines=[{
                "description": "Manufacturer hold-back",
                "account_id": cogs,
                "tax_code_id": gst,
                "quantity": Decimal("1"),
                "unit_price": Decimal("1200.00"),
                "discount_pct": Decimal("0"),
            }],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.post_credit_note(session, cn.id, posted_by="test")

    assert posted.status == CreditNoteStatus.POSTED
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, posted.journal_entry_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        lines = (
            await session.execute(
                select(JournalLine)
                .where(JournalLine.entry_id == entry.id)
                .order_by(JournalLine.line_no)
            )
        ).scalars().all()

        debits = sum((ln.debit for ln in lines), Decimal("0"))
        credits = sum((ln.credit for ln in lines), Decimal("0"))
        assert debits == credits, f"Journal imbalanced: Dr {debits} Cr {credits}"

        account_debits: dict[str, Decimal] = {}
        account_credits: dict[str, Decimal] = {}
        for ln in lines:
            acct = await session.get(Account, ln.account_id)
            assert acct is not None
            if ln.debit > 0:
                account_debits[acct.code] = account_debits.get(acct.code, Decimal("0")) + ln.debit
            if ln.credit > 0:
                account_credits[acct.code] = account_credits.get(acct.code, Decimal("0")) + ln.credit

        # AR debited (Dr AR — manufacturer owes dealer the hold-back)
        assert account_debits.get("1-1200") == Decimal("1320.00"), (
            f"Expected Dr AR 1320.00 but got {account_debits}"
        )
        # COGS credited (reduces cost of sale — contra-COGS)
        assert account_credits.get("5-2000") == Decimal("1200.00"), (
            f"Expected Cr COGS 1200.00 but got {account_credits}"
        )
        # G1 (income accounts) must be untouched: verify no income-type account in GL
        from saebooks.models.account import AccountType as AT
        for ln in lines:
            acct_obj = await session.get(Account, ln.account_id)
            assert acct_obj is not None
            assert acct_obj.account_type not in (AT.INCOME, AT.OTHER_INCOME), (
                f"Income account {acct_obj.code} should not appear in a COGS credit note GL"
            )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_prep_credit_note_counter")
async def test_post_rejects_mixed_income_and_cogs_lines() -> None:
    """Mixing INCOME and COGS lines on one credit note must be rejected at posting."""
    cid, contact, income, cogs, gst = await _ctx_with_cogs()
    async with AsyncSessionLocal() as session:
        cn = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=date(2026, 5, 1),
            lines=[
                {
                    "description": "Income line",
                    "account_id": income,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100.00"),
                    "discount_pct": Decimal("0"),
                },
                {
                    "description": "COGS line",
                    "account_id": cogs,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("50.00"),
                    "discount_pct": Decimal("0"),
                },
            ],
        )
    with pytest.raises(svc.CreditNoteError, match="Cannot mix income and COGS"):
        async with AsyncSessionLocal() as session:
            await svc.post_credit_note(session, cn.id, posted_by="test")
