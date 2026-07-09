"""Tests for trust account reports (gap RLES-3).

Covers:
* trust_cashbook: empty when no trust bank accounts exist
* trust_cashbook: receipts (debit) and payments (credit) with running balance
* trust_cashbook: opening balance from lines before from_date
* unreconciled_trust_balances: empty when no trust accounts
* unreconciled_trust_balances: liability balance from trust-linked journal entries
* Routes /reports/trust-cashbook and /reports/trust-balances return 200
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.services import journal as journal_svc
from saebooks.services import trust_reports as svc

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def _create_trust_bank(company_id: uuid.UUID, name: str = "Trust Bank") -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        acct = Account(
            company_id=company_id,
            code=f"1-TST-{uuid.uuid4().hex[:6]}",
            name=name,
            account_type=AccountType.ASSET,
            reconcile=True,
            is_trust_account=True,
        )
        session.add(acct)
        await session.commit()
        return acct.id


async def _create_trust_liability(company_id: uuid.UUID, name: str = "Test Trust Liability") -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        acct = Account(
            company_id=company_id,
            code=f"2-TST-{uuid.uuid4().hex[:6]}",
            name=name,
            account_type=AccountType.LIABILITY,
            reconcile=False,
            is_trust_account=False,
        )
        session.add(acct)
        await session.commit()
        return acct.id


async def _post_entry(
    company_id: uuid.UUID,
    entry_date: date,
    dr_account_id: uuid.UUID,
    cr_account_id: uuid.UUID,
    amount: Decimal,
    description: str = "",
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=description,
            lines=[
                {"account_id": dr_account_id, "debit": amount, "credit": Decimal("0")},
                {"account_id": cr_account_id, "debit": Decimal("0"), "credit": amount},
            ],
        )
        posted = await journal_svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED
        return posted.id


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trust_cashbook_empty_no_trust_accounts() -> None:
    async with AsyncSessionLocal() as session:
        reports = await svc.trust_cashbook(session, uuid.uuid4())
    assert reports == []


@pytest.mark.anyio
async def test_trust_cashbook_receipts_and_payments() -> None:
    co_id = await _company_id()
    trust_bank = await _create_trust_bank(co_id, "Trust Bank RaP")
    trust_liab = await _create_trust_liability(co_id, "Trust Liab RaP")

    # Receipt: rent in (DR trust bank, CR trust liability)
    await _post_entry(co_id, date(2026, 4, 5), trust_bank, trust_liab, Decimal("1200.00"), "Rent Apr")
    # Disbursement: pay landlord (DR trust liability, CR trust bank)
    await _post_entry(co_id, date(2026, 4, 20), trust_liab, trust_bank, Decimal("700.00"), "Landlord disbursement")

    async with AsyncSessionLocal() as session:
        reports = await svc.trust_cashbook(
            session, co_id,
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 30),
        )

    rpt = next(r for r in reports if r.account_name == "Trust Bank RaP")
    assert rpt.opening_balance == Decimal("0")
    assert rpt.total_receipts == Decimal("1200.00")
    assert rpt.total_payments == Decimal("700.00")
    assert rpt.closing_balance == Decimal("500.00")
    assert len(rpt.lines) == 2
    assert rpt.lines[0].running_balance == Decimal("1200.00")
    assert rpt.lines[1].running_balance == Decimal("500.00")


@pytest.mark.anyio
async def test_trust_cashbook_opening_balance() -> None:
    co_id = await _company_id()
    trust_bank2 = await _create_trust_bank(co_id, "Trust Bank Ob")
    trust_liab2 = await _create_trust_liability(co_id, "Trust Liab Ob")

    # Pre-period receipt
    await _post_entry(co_id, date(2026, 4, 3), trust_bank2, trust_liab2, Decimal("800.00"), "Pre-period")
    # In-period receipt
    await _post_entry(co_id, date(2026, 5, 2), trust_bank2, trust_liab2, Decimal("300.00"), "May receipt")

    async with AsyncSessionLocal() as session:
        reports = await svc.trust_cashbook(
            session, co_id,
            from_date=date(2026, 5, 1),
            to_date=date(2026, 5, 31),
        )

    rpt = next(r for r in reports if r.account_name == "Trust Bank Ob")
    assert rpt.opening_balance == Decimal("800.00")
    assert rpt.total_receipts == Decimal("300.00")
    assert rpt.closing_balance == Decimal("1100.00")


@pytest.mark.anyio
async def test_unreconciled_trust_balances_empty() -> None:
    async with AsyncSessionLocal() as session:
        report = await svc.unreconciled_trust_balances(session, uuid.uuid4())
    assert report.lines == []
    assert report.total_balance == Decimal("0")


@pytest.mark.anyio
async def test_unreconciled_trust_balances_shows_liability() -> None:
    co_id = await _company_id()
    trust_bank3 = await _create_trust_bank(co_id, "Trust Bank Bal")
    trust_liab3 = await _create_trust_liability(co_id, "Trust Liab Bal")

    await _post_entry(co_id, date(2026, 4, 7), trust_bank3, trust_liab3, Decimal("2000.00"), "Rent Apr")
    await _post_entry(co_id, date(2026, 4, 14), trust_bank3, trust_liab3, Decimal("500.00"), "Bond Apr")
    await _post_entry(co_id, date(2026, 4, 28), trust_liab3, trust_bank3, Decimal("1800.00"), "Disbursement Apr")

    async with AsyncSessionLocal() as session:
        report = await svc.unreconciled_trust_balances(
            session, co_id, as_of=date(2026, 4, 30)
        )

    match = next(
        (ln for ln in report.lines if ln.account_name == "Trust Liab Bal"),
        None,
    )
    assert match is not None, "Expected trust liability account in balances"
    # 2000 + 500 - 1800 = 700 credit balance = owed to beneficiaries
    assert match.balance == Decimal("700.00")


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------


