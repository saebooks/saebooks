"""Tests for ``saebooks.services.reports.budget_vs_actual``.

Covers:

* Budget-only row (no actual yet) reads as budget populated, actual zero.
* Actual-only row (no budget) reads as actual populated, budget zero.
* Budget + actual together produce the expected variance.
* Income accounts are sign-flipped so both budgets and actuals read
  as positive when trending "as planned".
* Router renders year-picker + summary 200.

Uses sentinel year 2099 so nothing real gets polluted.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.budget import Budget
from saebooks.models.company import Company
from saebooks.models.journal import JournalEntry
from saebooks.services import budgets as budget_svc
from saebooks.services import journal as journal_svc
from saebooks.services import reports as svc
pytestmark = pytest.mark.postgres_only

TEST_YEAR = 2099


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        async def _first_of(t: AccountType) -> uuid.UUID:
            acct = (
                await session.execute(
                    select(Account)
                    .where(
                        Account.company_id == company.id,
                        Account.account_type == t,
                        Account.is_header.is_(False),
                    )
                    .order_by(Account.code)
                )
            ).scalars().first()
            assert acct is not None
            return acct.id

        return (
            company.id,
            await _first_of(AccountType.INCOME),
            await _first_of(AccountType.EXPENSE),
        )


async def _post_expense_je(
    company_id: uuid.UUID,
    entry_date: date,
    *,
    expense_id: uuid.UUID,
    income_id: uuid.UUID,
    amount: Decimal,
) -> None:
    """Post a balanced JE: Dr Expense / Cr Income for amount.

    Using Income as the other leg means a single JE touches both
    test accounts so we get coverage of both sign conventions.
    """
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"bva-test {amount}",
            lines=[
                {
                    "account_id": expense_id,
                    "debit": amount,
                    "credit": Decimal("0"),
                },
                {
                    "account_id": income_id,
                    "debit": Decimal("0"),
                    "credit": amount,
                },
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="tests")


async def test_budget_only_no_actual() -> None:
    cid, income_id, _ = await _ctx()
    async with AsyncSessionLocal() as session:
        await budget_svc.upsert(
            session, cid,
            account_id=income_id, year=TEST_YEAR, month=3,
            amount=Decimal("500"),
        )

    async with AsyncSessionLocal() as session:
        report = await svc.budget_vs_actual(session, cid, year=TEST_YEAR)
    row = next(r for r in report.rows if r.account_id == income_id)
    assert row.budget_monthly[2] == Decimal("500")
    assert row.actual_monthly[2] == Decimal("0")
    assert row.variance_ytd == -row.budget_ytd  # actual(0) - budget(500)


async def test_actual_only_no_budget() -> None:
    cid, income_id, expense_id = await _ctx()
    await _post_expense_je(
        cid, date(TEST_YEAR, 5, 10),
        expense_id=expense_id, income_id=income_id,
        amount=Decimal("250"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.budget_vs_actual(session, cid, year=TEST_YEAR)
    expense_row = next(r for r in report.rows if r.account_id == expense_id)
    assert expense_row.actual_monthly[4] == Decimal("250")
    assert expense_row.budget_monthly[4] == Decimal("0")


async def test_variance_math_matches() -> None:
    cid, income_id, expense_id = await _ctx()
    # Budget $400 in August for expense
    async with AsyncSessionLocal() as session:
        await budget_svc.upsert(
            session, cid,
            account_id=expense_id, year=TEST_YEAR, month=8,
            amount=Decimal("400"),
        )
    # Actual $450 in August on expense — over-budget by 50
    await _post_expense_je(
        cid, date(TEST_YEAR, 8, 20),
        expense_id=expense_id, income_id=income_id,
        amount=Decimal("450"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.budget_vs_actual(session, cid, year=TEST_YEAR)
    row = next(r for r in report.rows if r.account_id == expense_id)
    assert row.budget_monthly[7] == Decimal("400")
    assert row.actual_monthly[7] == Decimal("450")
    variances = row.variance_monthly
    assert variances[7] == Decimal("50")
    assert row.variance_ytd >= Decimal("50")  # may include other months


async def test_income_account_is_credit_positive() -> None:
    """Income is credit-normal; the report flips the sign so it reads positive."""
    cid, income_id, expense_id = await _ctx()
    await _post_expense_je(
        cid, date(TEST_YEAR, 2, 5),
        expense_id=expense_id, income_id=income_id,
        amount=Decimal("1000"),
    )
    async with AsyncSessionLocal() as session:
        report = await svc.budget_vs_actual(session, cid, year=TEST_YEAR)
    row = next(r for r in report.rows if r.account_id == income_id)
    # Income got $1000 credit — should show +1000, not -1000
    assert row.actual_monthly[1] == Decimal("1000")


async def test_totals_sum_across_accounts() -> None:
    cid, _income, expense_id = await _ctx()
    async with AsyncSessionLocal() as session:
        await budget_svc.upsert(
            session, cid,
            account_id=expense_id, year=TEST_YEAR, month=6,
            amount=Decimal("100"),
        )
    async with AsyncSessionLocal() as session:
        report = await svc.budget_vs_actual(session, cid, year=TEST_YEAR)
    # budget_totals[5] should include the 100 we just wrote (possibly
    # alongside others from earlier tests in this module)
    assert report.budget_totals[5] >= Decimal("100")


async def test_budget_vs_actual_router_renders(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get(f"/reports/budget-vs-actual?year={TEST_YEAR}")
    assert r.status_code == 200
    assert "Budget vs actual" in r.text
    assert str(TEST_YEAR) in r.text


async def test_budget_vs_actual_index_card(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.get("/reports")
    assert r.status_code == 200
    assert "/reports/budget-vs-actual" in r.text


# ---------------------------------------------------------------------- #
# Cleanup                                                                 #
# ---------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
async def _clean_test_year() -> AsyncGenerator[None, None]:
    """Purge sentinel-year budgets + bva-test journal entries
    before AND after every test so the persistent dev DB stays clean
    and ordering between tests doesn't matter.
    """
    cid, _, _ = await _ctx()
    await _purge(cid)
    yield
    await _purge(cid)


async def _purge(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(Budget).where(
                Budget.company_id == company_id,
                Budget.year == TEST_YEAR,
            )
        )
        entries = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.company_id == company_id,
                    JournalEntry.description.like("bva-test%"),
                )
            )
        ).scalars().all()
        for e in entries:
            await session.delete(e)
        await session.commit()
