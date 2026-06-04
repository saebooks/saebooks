"""P0-4: N:1 bank reconciliation via the bsl_matches junction table.

Pre-fix the bank_statement_lines table had matched_to_* columns that
only supported one match per BSL. Real bookkeeping needs N:1 — a
single Medicare batch EFT might pay 30 invoices in one $5,000
transfer, and each of those needs its own allocation row so the
GL ledger reflects what actually happened.

These tests exercise the service directly (no HTTP layer), focused
on behaviour of the junction:

* Three partial matches summing to BSL.amount → MATCHED
* Two of three → PARTIAL; remove one → still PARTIAL with smaller total
* Remove all → UNMATCHED, legacy 1:1 columns cleared
* Sign-mismatch (positive allocation against negative BSL) raises ValueError
* Over-allocation raises ValueError
* Cross-company target_id raises CrossCompanyError
* IGNORED is sticky — recompute won't overwrite it
* Legacy match_line() still results in MATCHED + exactly one junction row
* unmatch_line() archives every junction row for that BSL
"""
from __future__ import annotations

import datetime as _dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.bsl_match import (
    TARGET_JOURNAL_ENTRY,
    BslMatch,
)
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services import reconciliation as recon_svc

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------- #
# Helpers / fixtures                                               #
# ---------------------------------------------------------------- #


async def _primary_company() -> Company:
    async with AsyncSessionLocal() as session:
        primary = (
            await session.execute(
                select(Company)
                .where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                )
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert primary is not None
    return primary


async def _expense_account(company_id: uuid.UUID) -> Account:
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company_id,
                    Account.account_type == AccountType.EXPENSE,
                    Account.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalars().first()
    assert acct is not None, "test DB has no EXPENSE account"
    return acct


async def _create_bank_account(company_id: uuid.UUID) -> Account:
    """Create a reconcilable bank/cash asset account."""
    async with AsyncSessionLocal() as session:
        acct = Account(
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            code=f"P04-{uuid.uuid4().hex[:6].upper()}",
            name="P0-4 N:1 test bank",
            account_type=AccountType.ASSET,
            reconcile=True,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
    return acct


async def _create_bsl(
    company_id: uuid.UUID, account_id: uuid.UUID, amount: Decimal
) -> BankStatementLine:
    async with AsyncSessionLocal() as session:
        bsl = BankStatementLine(
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            account_id=account_id,
            txn_date=_dt.date(2026, 4, 1),
            amount=amount,
            description=f"P0-4 BSL {uuid.uuid4().hex[:6]}",
        )
        session.add(bsl)
        await session.commit()
        await session.refresh(bsl)
    return bsl


async def _create_posted_entry(
    company_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    expense_account_id: uuid.UUID,
    amount: Decimal,
) -> JournalEntry:
    """Create a posted journal entry whose bank-side debit == amount."""
    async with AsyncSessionLocal() as session:
        entry = JournalEntry(
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            ref=f"P04-{uuid.uuid4().hex[:6]}",
            entry_date=_dt.date(2026, 4, 1),
            description="P0-4 posted entry",
            status=EntryStatus.POSTED,
            posted_by="p04-test",
        )
        session.add(entry)
        await session.flush()
        session.add(
            JournalLine(
                entry_id=entry.id,
                line_no=1,
                account_id=bank_account_id,
                debit=amount,
                credit=Decimal("0"),
            )
        )
        session.add(
            JournalLine(
                entry_id=entry.id,
                line_no=2,
                account_id=expense_account_id,
                debit=Decimal("0"),
                credit=amount,
            )
        )
        await session.commit()
        await session.refresh(entry)
    return entry


async def _live_match_count(bsl_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BslMatch).where(
                    BslMatch.bsl_id == bsl_id,
                    BslMatch.archived_at.is_(None),
                )
            )
        ).scalars().all()
    return len(rows)


# ---------------------------------------------------------------- #
# Tests                                                            #
# ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_three_partial_matches_sum_to_matched() -> None:
    """3 allocations of $100 against a $300 deposit → status MATCHED."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("300.00"))

    e1 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))
    e2 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))
    e3 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))

    async with AsyncSessionLocal() as session:
        await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=e1.id, amount=Decimal("100.00"), matched_by="p04-test",
        )
        await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=e2.id, amount=Decimal("100.00"), matched_by="p04-test",
        )
        await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=e3.id, amount=Decimal("100.00"), matched_by="p04-test",
        )

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.MATCHED
    assert await _live_match_count(bsl.id) == 3


@pytest.mark.asyncio
async def test_two_of_three_partial_then_remove_decays() -> None:
    """2 of 3 added → PARTIAL; remove one → PARTIAL with smaller total."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("300.00"))

    e1 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))
    e2 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))

    async with AsyncSessionLocal() as session:
        m1 = await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=e1.id, amount=Decimal("100.00"),
        )
        await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=e2.id, amount=Decimal("100.00"),
        )

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.PARTIAL
    assert await _live_match_count(bsl.id) == 2

    async with AsyncSessionLocal() as session:
        await recon_svc.remove_match(session, m1.id)

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.PARTIAL
    assert await _live_match_count(bsl.id) == 1


@pytest.mark.asyncio
async def test_remove_all_resets_to_unmatched_and_clears_legacy_cols() -> None:
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("100.00"))
    entry = await _create_posted_entry(
        company.id, bank.id, expense.id, Decimal("100.00")
    )

    async with AsyncSessionLocal() as session:
        m = await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=entry.id, amount=Decimal("100.00"),
        )

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.MATCHED
    assert refreshed.matched_entry_id == entry.id

    async with AsyncSessionLocal() as session:
        await recon_svc.remove_match(session, m.id)

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.UNMATCHED
    assert refreshed.matched_entry_id is None
    assert refreshed.matched_to_type is None
    assert refreshed.matched_to_id is None


@pytest.mark.asyncio
async def test_sign_mismatch_rejected() -> None:
    """Positive allocation against a negative (withdrawal) BSL must raise."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    # Withdrawal: -100.
    bsl = await _create_bsl(company.id, bank.id, Decimal("-100.00"))
    entry = await _create_posted_entry(
        company.id, bank.id, expense.id, Decimal("100.00")
    )

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="sign"):
            await recon_svc.add_match(
                session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
                target_id=entry.id, amount=Decimal("100.00"),  # wrong sign
            )


@pytest.mark.asyncio
async def test_over_allocation_rejected() -> None:
    """Sum of allocations cannot exceed |BSL.amount| beyond the tolerance."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("100.00"))
    e1 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))
    e2 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))

    async with AsyncSessionLocal() as session:
        await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=e1.id, amount=Decimal("100.00"),
        )

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="over-allocate"):
            await recon_svc.add_match(
                session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
                target_id=e2.id, amount=Decimal("50.00"),  # 100+50 > 100
            )


@pytest.mark.asyncio
async def test_cross_company_target_rejected() -> None:
    """A target_id owned by another company must raise CrossCompanyError."""
    # Create a sibling company, post an entry into it, then try to match
    # the primary company's BSL against the sibling's entry.
    primary = await _primary_company()
    bank = await _create_bank_account(primary.id)
    await _expense_account(primary.id)
    bsl = await _create_bsl(primary.id, bank.id, Decimal("100.00"))

    async with AsyncSessionLocal() as session:
        sibling = Company(
            name=f"P04-Sibling-{uuid.uuid4().hex[:6]}",
            base_currency="AUD",
            tenant_id=DEFAULT_TENANT_ID,
        )
        session.add(sibling)
        await session.commit()
        await session.refresh(sibling)
        sibling_id = sibling.id

    sibling_bank = await _create_bank_account(sibling_id)
    sibling_expense_acct = Account(
        company_id=sibling_id,
        tenant_id=DEFAULT_TENANT_ID,
        code=f"P04X-{uuid.uuid4().hex[:6].upper()}",
        name="sibling expense",
        account_type=AccountType.EXPENSE,
    )
    async with AsyncSessionLocal() as session:
        session.add(sibling_expense_acct)
        await session.commit()
        await session.refresh(sibling_expense_acct)
    sibling_entry = await _create_posted_entry(
        sibling_id, sibling_bank.id, sibling_expense_acct.id, Decimal("100.00")
    )

    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValueError, match="not found"):
                await recon_svc.add_match(
                    session,
                    bsl_id=bsl.id,
                    target_type=TARGET_JOURNAL_ENTRY,
                    target_id=sibling_entry.id,
                    amount=Decimal("100.00"),
                )
    finally:
        # Soft-delete the sibling so other tests don't see it.
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func
            sib = await session.get(Company, sibling_id)
            if sib is not None:
                sib.archived_at = func.now()
                await session.commit()


@pytest.mark.asyncio
async def test_ignored_status_is_sticky() -> None:
    """recompute must not overwrite a manually-set IGNORED."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("100.00"))
    entry = await _create_posted_entry(
        company.id, bank.id, expense.id, Decimal("100.00")
    )

    async with AsyncSessionLocal() as session:
        live = await session.get(BankStatementLine, bsl.id)
        live.status = StatementLineStatus.IGNORED
        await session.commit()

    async with AsyncSessionLocal() as session:
        await recon_svc.add_match(
            session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
            target_id=entry.id, amount=Decimal("100.00"),
        )

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.IGNORED


@pytest.mark.asyncio
async def test_legacy_match_line_writes_through_junction() -> None:
    """match_line() back-compat path produces exactly one junction row."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("250.00"))
    entry = await _create_posted_entry(
        company.id, bank.id, expense.id, Decimal("250.00")
    )

    async with AsyncSessionLocal() as session:
        await recon_svc.match_line(session, bsl.id, entry.id)

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.MATCHED
    assert refreshed.matched_entry_id == entry.id
    assert await _live_match_count(bsl.id) == 1


@pytest.mark.asyncio
async def test_unmatch_line_archives_every_match() -> None:
    """unmatch_line() must archive ALL live junction rows, even N:1."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("300.00"))
    e1 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))
    e2 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))
    e3 = await _create_posted_entry(company.id, bank.id, expense.id, Decimal("100.00"))

    async with AsyncSessionLocal() as session:
        for ent in (e1, e2, e3):
            await recon_svc.add_match(
                session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
                target_id=ent.id, amount=Decimal("100.00"),
            )

    assert await _live_match_count(bsl.id) == 3

    async with AsyncSessionLocal() as session:
        await recon_svc.unmatch_line(session, bsl.id)

    assert await _live_match_count(bsl.id) == 0
    async with AsyncSessionLocal() as session:
        refreshed = await session.get(BankStatementLine, bsl.id)
    assert refreshed.status == StatementLineStatus.UNMATCHED


@pytest.mark.asyncio
async def test_unposted_journal_entry_rejected() -> None:
    """match against a DRAFT entry must raise."""
    company = await _primary_company()
    bank = await _create_bank_account(company.id)
    expense = await _expense_account(company.id)
    bsl = await _create_bsl(company.id, bank.id, Decimal("100.00"))

    async with AsyncSessionLocal() as session:
        entry = JournalEntry(
            company_id=company.id,
            tenant_id=DEFAULT_TENANT_ID,
            ref=f"P04-DRAFT-{uuid.uuid4().hex[:6]}",
            entry_date=_dt.date(2026, 4, 1),
            description="P0-4 draft",
            status=EntryStatus.DRAFT,
        )
        session.add(entry)
        await session.flush()
        session.add(JournalLine(
            entry_id=entry.id, line_no=1, account_id=bank.id,
            debit=Decimal("100.00"), credit=Decimal("0"),
        ))
        session.add(JournalLine(
            entry_id=entry.id, line_no=2, account_id=expense.id,
            debit=Decimal("0"), credit=Decimal("100.00"),
        ))
        await session.commit()
        entry_id = entry.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="posted"):
            await recon_svc.add_match(
                session, bsl_id=bsl.id, target_type=TARGET_JOURNAL_ENTRY,
                target_id=entry_id, amount=Decimal("100.00"),
            )
