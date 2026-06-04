"""Tests for ``saebooks.services.budgets``.

Budgets are a reporting overlay — the stakes are:

1. `upsert` writes once then updates in place (no duplicate key).
2. `bulk_upsert` handles a whole-year grid atomically + deletes any
   zero-amount row (unset UX).
3. `list_for_period` filters by year / month window / account.
4. CHECK constraint rejects month=0 or month=13.
5. Zero-amount row deletion via bulk_upsert is idempotent.

Tests run against the live AU-seeded DB; budget rows are keyed by
`(company_id, account_id, year, month)` and tests use a test year
`BUDGET_TEST_YEAR=2099` so real years never get polluted.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.budget import Budget
from saebooks.models.company import Company
from saebooks.services import budgets as svc

pytestmark = pytest.mark.postgres_only

TEST_YEAR = 2099  # far-future sentinel so nothing real collides


async def _context() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, income_account_id, expense_account_id)."""
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


async def _purge(company_id: uuid.UUID) -> None:
    """Wipe the test year rows so tests are order-independent."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(Budget).where(
                Budget.company_id == company_id,
                Budget.year == TEST_YEAR,
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_test_year() -> AsyncGenerator[None, None]:
    """Purge the sentinel year both before and after every test."""
    cid, _, _ = await _context()
    await _purge(cid)
    yield
    await _purge(cid)


async def test_upsert_insert_then_update() -> None:
    cid, income_id, _ = await _context()
    async with AsyncSessionLocal() as session:
        row_a = await svc.upsert(
            session, cid,
            account_id=income_id, year=TEST_YEAR, month=3,
            amount=Decimal("100.00"),
        )
    assert row_a.amount == Decimal("100.00")

    async with AsyncSessionLocal() as session:
        row_b = await svc.upsert(
            session, cid,
            account_id=income_id, year=TEST_YEAR, month=3,
            amount=Decimal("250.50"),
            notes="revised",
        )
    assert row_b.id == row_a.id  # same row, upserted in place
    assert row_b.amount == Decimal("250.50")
    assert row_b.notes == "revised"


async def test_upsert_rejects_month_out_of_range() -> None:
    cid, income_id, _ = await _context()
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="out of range"):
            await svc.upsert(
                session, cid,
                account_id=income_id, year=TEST_YEAR, month=0,
                amount=Decimal("1"),
            )
        with pytest.raises(ValueError, match="out of range"):
            await svc.upsert(
                session, cid,
                account_id=income_id, year=TEST_YEAR, month=13,
                amount=Decimal("1"),
            )


async def test_list_for_period_returns_year_window() -> None:
    cid, income_id, expense_id = await _context()
    async with AsyncSessionLocal() as session:
        await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR, month=1,
            amount=Decimal("1000"),
        )
        await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR, month=6,
            amount=Decimal("1500"),
        )
        await svc.upsert(
            session, cid, account_id=expense_id, year=TEST_YEAR, month=6,
            amount=Decimal("300"),
        )
        # Different year should NOT appear
        await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR - 1, month=6,
            amount=Decimal("9999"),
        )

    async with AsyncSessionLocal() as session:
        rows = await svc.list_for_period(session, cid, year=TEST_YEAR)
    assert len(rows) == 3
    assert all(r.year == TEST_YEAR for r in rows)

    # Clean up the off-year row we stashed so other tests don't trip
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(Budget).where(
                Budget.company_id == cid,
                Budget.year == TEST_YEAR - 1,
            )
        )
        await session.commit()


async def test_list_for_period_month_window_and_account_filter() -> None:
    cid, income_id, expense_id = await _context()
    async with AsyncSessionLocal() as session:
        for m, a, amt in [
            (2, income_id, Decimal("100")),
            (5, income_id, Decimal("200")),
            (8, income_id, Decimal("300")),
            (5, expense_id, Decimal("50")),
        ]:
            await svc.upsert(
                session, cid,
                account_id=a, year=TEST_YEAR, month=m, amount=amt,
            )

    async with AsyncSessionLocal() as session:
        # Q2 window (Apr-Jun) → just month 5 for both accounts
        q2 = await svc.list_for_period(
            session, cid, year=TEST_YEAR, month_from=4, month_to=6,
        )
    assert {r.month for r in q2} == {5}
    assert len(q2) == 2  # income + expense

    async with AsyncSessionLocal() as session:
        # Account filter → only income rows, all months
        income_only = await svc.list_for_period(
            session, cid, year=TEST_YEAR, account_id=income_id,
        )
    assert {r.month for r in income_only} == {2, 5, 8}
    assert all(r.account_id == income_id for r in income_only)


async def test_bulk_upsert_writes_twelve_months() -> None:
    cid, income_id, _ = await _context()
    rows = [
        {
            "account_id": income_id,
            "month": m,
            "amount": Decimal("100.00") * m,
            "notes": f"m{m}",
        }
        for m in range(1, 13)
    ]
    async with AsyncSessionLocal() as session:
        written = await svc.bulk_upsert(session, cid, year=TEST_YEAR, rows=rows)
    assert written == 12

    async with AsyncSessionLocal() as session:
        stored = await svc.list_for_period(
            session, cid, year=TEST_YEAR, account_id=income_id,
        )
    assert len(stored) == 12
    total = sum((r.amount for r in stored), Decimal("0"))
    assert total == Decimal("7800.00")  # 100 * (1+2+...+12) = 7800


async def test_bulk_upsert_zero_amount_deletes() -> None:
    cid, income_id, _ = await _context()
    # Seed a row
    async with AsyncSessionLocal() as session:
        await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR, month=4,
            amount=Decimal("500"),
        )

    # Now bulk-upsert with month 4 zero → deletion
    rows = [
        {"account_id": income_id, "month": 4, "amount": Decimal("0"), "notes": None},
        {"account_id": income_id, "month": 5, "amount": Decimal("200"), "notes": None},
    ]
    async with AsyncSessionLocal() as session:
        written = await svc.bulk_upsert(session, cid, year=TEST_YEAR, rows=rows)
    assert written == 2

    async with AsyncSessionLocal() as session:
        stored = await svc.list_for_period(
            session, cid, year=TEST_YEAR, account_id=income_id,
        )
    assert {r.month for r in stored} == {5}


async def test_bulk_upsert_is_idempotent() -> None:
    cid, income_id, _ = await _context()
    rows = [
        {"account_id": income_id, "month": 1, "amount": Decimal("1000"), "notes": None},
        {"account_id": income_id, "month": 2, "amount": Decimal("2000"), "notes": None},
    ]
    async with AsyncSessionLocal() as session:
        await svc.bulk_upsert(session, cid, year=TEST_YEAR, rows=rows)
        # Second call with same payload must not create duplicates
        await svc.bulk_upsert(session, cid, year=TEST_YEAR, rows=rows)

    async with AsyncSessionLocal() as session:
        stored = await svc.list_for_period(
            session, cid, year=TEST_YEAR, account_id=income_id,
        )
    assert len(stored) == 2
    amounts = {r.month: r.amount for r in stored}
    assert amounts == {1: Decimal("1000.00"), 2: Decimal("2000.00")}


async def test_bulk_upsert_empty_rows_noop() -> None:
    cid, _, _ = await _context()
    async with AsyncSessionLocal() as session:
        written = await svc.bulk_upsert(session, cid, year=TEST_YEAR, rows=[])
    assert written == 0


async def test_bulk_upsert_rejects_bad_month() -> None:
    cid, income_id, _ = await _context()
    rows = [
        {"account_id": income_id, "month": 13, "amount": Decimal("100"), "notes": None},
    ]
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="out of range"):
            await svc.bulk_upsert(session, cid, year=TEST_YEAR, rows=rows)


async def test_delete_budget_by_id() -> None:
    cid, income_id, _ = await _context()
    async with AsyncSessionLocal() as session:
        row = await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR, month=7,
            amount=Decimal("1"),
        )

    async with AsyncSessionLocal() as session:
        await svc.delete_budget(session, row.id)

    async with AsyncSessionLocal() as session:
        assert await svc.get(session, row.id) is None


async def test_delete_budget_missing_is_noop() -> None:
    async with AsyncSessionLocal() as session:
        # Should not raise
        await svc.delete_budget(session, uuid.uuid4())


async def test_upsert_notes_can_be_cleared_by_none() -> None:
    cid, income_id, _ = await _context()
    async with AsyncSessionLocal() as session:
        await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR, month=9,
            amount=Decimal("100"), notes="forecast",
        )
        row = await svc.upsert(
            session, cid, account_id=income_id, year=TEST_YEAR, month=9,
            amount=Decimal("100"), notes=None,
        )
    assert row.notes is None
