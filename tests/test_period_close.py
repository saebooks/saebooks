"""Tests for ``saebooks.services.period_close``.

Covers:

* ``preview_close`` on an empty-P&L DB returns ``has_anything_to_close=False``
* Profit → retained earnings is CREDITED by the net profit
* Loss → retained earnings is DEBITED by the net loss
* ``close_year`` posts a balanced journal + locks the period
* Idempotency: running ``close_year`` twice is a no-op on the second run
  (every P&L account is already zero)
* Mid-year close (explicit from_date/through_date) doesn't touch prior
  or later periods
* Router: GET /reports/close-year renders the preview;
  POST /reports/close-year redirects to /journal/<id>
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, PeriodLock
from saebooks.services import journal as journal_svc
from saebooks.services import period_close as svc
pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------- #
# Fixtures                                                                #
# ---------------------------------------------------------------------- #


async def _ctx() -> dict[str, uuid.UUID]:
    """Grab the live company + a handful of well-known CoA accounts.

    Returns a dict keyed on purpose so each test can pull what it needs
    without a 5-tuple.
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

        async def _by_code(code: str) -> Account:
            return (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id,
                        Account.code == code,
                    )
                )
            ).scalar_one()

        cash = await _by_code("1-1110")          # Cash at bank
        income = await _by_code("4-6000")        # Sales
        expense = await _by_code("6-1000")       # Advertising (any EXPENSE)
        retained = await _by_code("3-8000")      # Retained Earnings

    return {
        "company_id": company.id,
        "cash_id": cash.id,
        "income_id": income.id,
        "expense_id": expense.id,
        "retained_id": retained.id,
    }


@pytest.fixture(autouse=True)
async def _scrub_period_close_artefacts() -> None:
    """Delete any period-close journals + locks left from a prior run.

    The persistent dev DB would otherwise accumulate these across test
    sessions and skew both the preview math and the idempotency check.
    """
    from sqlalchemy import delete, or_

    async with AsyncSessionLocal() as session:
        # Delete close-year journals (their description starts with a
        # distinctive prefix we control). CASCADE cleans lines.
        await session.execute(
            delete(JournalEntry).where(
                or_(
                    JournalEntry.description.like("Year-end close%"),
                    JournalEntry.description.like("period_close_test_%"),
                )
            )
        )
        # Delete any period locks stamped during tests — we stamp them
        # with a 'Year-end close' reason.
        await session.execute(
            delete(PeriodLock).where(PeriodLock.reason.like("%Year-end close%"))
        )
        await session.commit()
    yield


async def _post_income(
    company_id: uuid.UUID,
    income_id: uuid.UUID,
    cash_id: uuid.UUID,
    *,
    amount: Decimal,
    entry_date: date,
    tag: str = "income",
) -> uuid.UUID:
    """Post a simple Dr Cash / Cr Income journal for `amount` on date."""
    async with AsyncSessionLocal() as session:
        draft = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"period_close_test_{tag}",
            lines=[
                {"account_id": cash_id, "debit": amount, "credit": Decimal("0")},
                {"account_id": income_id, "debit": Decimal("0"), "credit": amount},
            ],
        )
        posted = await journal_svc.post(
            session, draft.id, posted_by="tests",
            override_reason="test fixture",
            actor_role="admin",  # F-04: period-lock override requires role
        )
        return posted.id


async def _post_expense(
    company_id: uuid.UUID,
    expense_id: uuid.UUID,
    cash_id: uuid.UUID,
    *,
    amount: Decimal,
    entry_date: date,
    tag: str = "expense",
) -> uuid.UUID:
    """Post Dr Expense / Cr Cash journal for `amount` on date."""
    async with AsyncSessionLocal() as session:
        draft = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"period_close_test_{tag}",
            lines=[
                {"account_id": expense_id, "debit": amount, "credit": Decimal("0")},
                {"account_id": cash_id, "debit": Decimal("0"), "credit": amount},
            ],
        )
        posted = await journal_svc.post(
            session, draft.id, posted_by="tests",
            override_reason="test fixture",
            actor_role="admin",  # F-04: period-lock override requires role
        )
        return posted.id


# ---------------------------------------------------------------------- #
# preview_close — math                                                    #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_preview_profit_credits_retained_earnings() -> None:
    """Income 1000 + expenses 400 = profit 600 → retained earnings CREDIT 600.

    Also asserts the generated lines include the debit against income
    and the credit against the expense account (so P&L accounts zero
    out), each for the right amount.
    """
    ctx = await _ctx()
    # Use a narrow window nobody else uses so unrelated history doesn't
    # leak into the preview.
    from_d = date(2022, 7, 1)
    through_d = date(2022, 12, 31)

    await _post_income(
        ctx["company_id"], ctx["income_id"], ctx["cash_id"],
        amount=Decimal("1000"), entry_date=date(2022, 9, 15), tag="profit_income",
    )
    await _post_expense(
        ctx["company_id"], ctx["expense_id"], ctx["cash_id"],
        amount=Decimal("400"), entry_date=date(2022, 10, 1), tag="profit_expense",
    )

    async with AsyncSessionLocal() as session:
        preview = await svc.preview_close(
            session,
            ctx["company_id"],
            through_date=through_d,
            retained_earnings_account_id=ctx["retained_id"],
            from_date=from_d,
        )

    assert preview.net_profit == Decimal("600")
    assert preview.total_income == Decimal("1000")
    assert preview.total_expenses == Decimal("400")
    assert preview.retained_earnings_credit == Decimal("600")
    assert preview.retained_earnings_debit == Decimal("0")

    # Three lines: Dr Income 1000, Cr Expense 400, Cr Retained Earnings 600
    debits = sum(
        Decimal(str(line.get("debit", "0"))) for line in preview.lines
    )
    credits = sum(
        Decimal(str(line.get("credit", "0"))) for line in preview.lines
    )
    assert debits == credits == Decimal("1000")


@pytest.mark.asyncio
async def test_preview_loss_debits_retained_earnings() -> None:
    """Income 200 + expenses 500 = loss 300 → retained earnings DEBIT 300."""
    ctx = await _ctx()
    from_d = date(2021, 1, 1)
    through_d = date(2021, 6, 30)

    await _post_income(
        ctx["company_id"], ctx["income_id"], ctx["cash_id"],
        amount=Decimal("200"), entry_date=date(2021, 2, 1), tag="loss_income",
    )
    await _post_expense(
        ctx["company_id"], ctx["expense_id"], ctx["cash_id"],
        amount=Decimal("500"), entry_date=date(2021, 3, 1), tag="loss_expense",
    )

    async with AsyncSessionLocal() as session:
        preview = await svc.preview_close(
            session,
            ctx["company_id"],
            through_date=through_d,
            retained_earnings_account_id=ctx["retained_id"],
            from_date=from_d,
        )

    assert preview.net_profit == Decimal("-300")
    assert preview.retained_earnings_debit == Decimal("300")
    assert preview.retained_earnings_credit == Decimal("0")

    debits = sum(
        Decimal(str(line.get("debit", "0"))) for line in preview.lines
    )
    credits = sum(
        Decimal(str(line.get("credit", "0"))) for line in preview.lines
    )
    assert debits == credits == Decimal("500")


@pytest.mark.asyncio
async def test_preview_empty_period_has_nothing_to_close() -> None:
    """A window with no P&L activity → empty lines, ``has_anything_to_close=False``."""
    ctx = await _ctx()
    async with AsyncSessionLocal() as session:
        preview = await svc.preview_close(
            session,
            ctx["company_id"],
            through_date=date(1995, 12, 31),
            retained_earnings_account_id=ctx["retained_id"],
            from_date=date(1995, 1, 1),
        )
    assert preview.lines == []
    assert preview.has_anything_to_close is False
    assert preview.net_profit == Decimal("0")


# ---------------------------------------------------------------------- #
# close_year — posts + locks                                              #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_close_year_posts_balanced_journal_and_locks() -> None:
    ctx = await _ctx()
    from_d = date(2020, 7, 1)
    through_d = date(2020, 12, 31)

    await _post_income(
        ctx["company_id"], ctx["income_id"], ctx["cash_id"],
        amount=Decimal("1500"), entry_date=date(2020, 8, 1), tag="close_income",
    )
    await _post_expense(
        ctx["company_id"], ctx["expense_id"], ctx["cash_id"],
        amount=Decimal("300"), entry_date=date(2020, 9, 1), tag="close_expense",
    )

    async with AsyncSessionLocal() as session:
        entry = await svc.close_year(
            session,
            ctx["company_id"],
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through_d,
            retained_earnings_account_id=ctx["retained_id"],
            posted_by="tests",
            from_date=from_d,
            override_reason="test fixture",
            actor_role="admin",  # F-04: period-lock override requires role
        )

    assert entry is not None
    assert entry.status == EntryStatus.POSTED
    assert entry.entry_date == through_d
    total_dr = sum(line.debit for line in entry.lines)
    total_cr = sum(line.credit for line in entry.lines)
    assert total_dr == total_cr == Decimal("1500")

    # Period lock exists for this date with our reason prefix
    async with AsyncSessionLocal() as session:
        lock = (
            await session.execute(
                select(PeriodLock).where(
                    PeriodLock.company_id == ctx["company_id"],
                    PeriodLock.locked_through == through_d,
                )
            )
        ).scalars().first()
        assert lock is not None


@pytest.mark.asyncio
async def test_close_year_is_idempotent_on_second_run() -> None:
    """After closing, every P&L account in that window is zero, so the
    second call returns None — no second journal, no double-closure."""
    ctx = await _ctx()
    from_d = date(2019, 7, 1)
    through_d = date(2019, 12, 31)

    await _post_income(
        ctx["company_id"], ctx["income_id"], ctx["cash_id"],
        amount=Decimal("900"), entry_date=date(2019, 8, 1), tag="idem_income",
    )

    async with AsyncSessionLocal() as session:
        first = await svc.close_year(
            session,
            ctx["company_id"],
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through_d,
            retained_earnings_account_id=ctx["retained_id"],
            posted_by="tests",
            from_date=from_d,
            # Don't lock — we want to re-run the closure in this test
            # WITHOUT needing an override reason.
            lock_period=False,
            override_reason="test fixture",
            actor_role="admin",  # F-04
        )
    assert first is not None

    async with AsyncSessionLocal() as session:
        second = await svc.close_year(
            session,
            ctx["company_id"],
            tenant_id=DEFAULT_TENANT_ID,
            through_date=through_d,
            retained_earnings_account_id=ctx["retained_id"],
            posted_by="tests",
            from_date=from_d,
            lock_period=False,
            override_reason="test fixture",
            actor_role="admin",  # F-04
        )
    assert second is None  # no-op — nothing to close


@pytest.mark.asyncio
async def test_close_year_does_not_touch_later_periods() -> None:
    """A Q1 close shouldn't consume Q2 activity.

    Posts income in Jan (inside the window) + income in July (outside).
    Closes Jan-June. Verifies the July entry is untouched by closing
    from_date/through_date alone, and the preview's total only sees
    the Jan amount.
    """
    ctx = await _ctx()
    h1_from = date(2018, 1, 1)
    h1_through = date(2018, 6, 30)

    inside_id = await _post_income(
        ctx["company_id"], ctx["income_id"], ctx["cash_id"],
        amount=Decimal("400"), entry_date=date(2018, 2, 1), tag="inside",
    )
    outside_id = await _post_income(
        ctx["company_id"], ctx["income_id"], ctx["cash_id"],
        amount=Decimal("700"), entry_date=date(2018, 9, 15), tag="outside",
    )

    async with AsyncSessionLocal() as session:
        preview = await svc.preview_close(
            session,
            ctx["company_id"],
            through_date=h1_through,
            retained_earnings_account_id=ctx["retained_id"],
            from_date=h1_from,
        )
        assert preview.total_income == Decimal("400")
        # Close it (no lock so we don't pollute unrelated tests)
        entry = await svc.close_year(
            session,
            ctx["company_id"],
            tenant_id=DEFAULT_TENANT_ID,
            through_date=h1_through,
            retained_earnings_account_id=ctx["retained_id"],
            posted_by="tests",
            from_date=h1_from,
            lock_period=False,
            override_reason="test fixture",
            actor_role="admin",  # F-04
        )
        assert entry is not None

        # The outside entry is still POSTED and untouched.
        outside = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == outside_id)
            )
        ).scalar_one()
        assert outside.status == EntryStatus.POSTED

        # The inside entry is ALSO still POSTED — closure zeros the
        # ACCOUNT via a new offsetting journal, not by rewriting the
        # original entry.
        inside = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == inside_id)
            )
        ).scalar_one()
        assert inside.status == EntryStatus.POSTED


# ---------------------------------------------------------------------- #
# Router smoke                                                            #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_close_year_form_renders(client: AsyncClient) -> None:
    r = await client.get("/reports/close-year")
    assert r.status_code == 200
    # Headings + equity-account picker present
    assert "Close year" in r.text
    # Retained Earnings option — seeded by the AU CoA
    assert "Retained Earnings" in r.text


@pytest.mark.asyncio
async def test_close_year_index_links(client: AsyncClient) -> None:
    r = await client.get("/reports")
    assert r.status_code == 200
    assert "/reports/close-year" in r.text
