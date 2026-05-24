"""Diminishing-value depreciation math tests (Batch MM/1).

Covers the pure ``_cumulative_dv`` helper (no DB, fast) plus one
DB-integration test that posts DV depreciation end-to-end through
``post_depreciation`` to prove the method dispatch in
``cumulative_depreciation_through`` routes correctly.

DV model: each month charges ``book_value * (rate / 100) / 12``,
book value depleting each month. First and last months prorated by
day-count. Floors at residual.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.services import assets as svc
from saebooks.services.assets import _cumulative_dv
import pytest
pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------- #
# Pure helper (no DB)                                                    #
# ---------------------------------------------------------------------- #


def test_dv_before_in_service_returns_zero() -> None:
    result = _cumulative_dv(
        cost=Decimal("10000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 6, 1),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 5, 31),
    )
    assert result == Decimal("0")


def test_dv_exact_first_month_full() -> None:
    # 10000 * 30% / 12 = 250 per month on first month (full January).
    result = _cumulative_dv(
        cost=Decimal("10000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 1, 1),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 1, 31),
    )
    assert result == Decimal("250.00")


def test_dv_month_two_uses_new_book_value() -> None:
    # Month 1: 10000 @ 2.5% = 250. Book value now 9750.
    # Month 2: 9750 @ 2.5% = 243.75. Cumulative = 493.75.
    result = _cumulative_dv(
        cost=Decimal("10000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 1, 1),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 2, 28),
    )
    assert result == Decimal("493.75")


def test_dv_first_month_partial_prorated() -> None:
    # In-service Jan 15: 17 of 31 days active. Book * 2.5% * (17/31).
    # 10000 * 0.025 * 17/31 = 137.0967... → 137.10 (HALF_UP).
    result = _cumulative_dv(
        cost=Decimal("10000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 1, 15),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 1, 31),
    )
    assert result == Decimal("137.10")


def test_dv_three_months_compounds_book_value() -> None:
    # Full Jan/Feb/Mar at 30% DV on $10k.
    # Jan: 10000 * 0.025 = 250 → book 9750
    # Feb: 9750 * 0.025 = 243.75 → book 9506.25
    # Mar: 9506.25 * 0.025 = 237.65625 → 237.66 (HALF_UP)
    # Total ≈ 731.41
    result = _cumulative_dv(
        cost=Decimal("10000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 1, 1),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 3, 31),
    )
    assert result == Decimal("731.41")


def test_dv_residual_floor_never_undershoots() -> None:
    # 100% DV over 10 years with residual 500 — book value should
    # never drop below 500 no matter how far we project.
    result = _cumulative_dv(
        cost=Decimal("1000"),
        residual=Decimal("500"),
        in_service_date=date(2020, 1, 1),
        annual_rate_pct=Decimal("100"),
        through=date(2040, 12, 31),
    )
    assert result == Decimal("500.00")
    # And depreciation stops cleanly once residual is hit — one more
    # year shouldn't inflate it.
    more = _cumulative_dv(
        cost=Decimal("1000"),
        residual=Decimal("500"),
        in_service_date=date(2020, 1, 1),
        annual_rate_pct=Decimal("100"),
        through=date(2050, 12, 31),
    )
    assert more == Decimal("500.00")


def test_dv_zero_base_returns_zero() -> None:
    # Cost equals residual → depreciable base is zero.
    result = _cumulative_dv(
        cost=Decimal("1000"),
        residual=Decimal("1000"),
        in_service_date=date(2026, 1, 1),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 12, 31),
    )
    assert result == Decimal("0")


def test_dv_low_rate_five_pct_over_one_year() -> None:
    # 5% DV over a full year on $20k residual 0.
    # 12 months compounding: book * (1 - 0.05/12)^12 ≈ 20000 * 0.95116...
    # Cumulative dep ≈ 20000 * (1 - 0.95116...) ≈ 976.74
    result = _cumulative_dv(
        cost=Decimal("20000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 1, 1),
        annual_rate_pct=Decimal("5"),
        through=date(2026, 12, 31),
    )
    # Loose bound — exact value depends on month-by-month quantize order.
    assert Decimal("970") < result < Decimal("985")


def test_dv_month_fraction_through_midmonth() -> None:
    # In-service Jan 1, through Jan 15 → 15/31 of Jan charge.
    # 10000 * 0.025 * 15/31 = 120.967... → 120.97 (HALF_UP).
    result = _cumulative_dv(
        cost=Decimal("10000"),
        residual=Decimal("0"),
        in_service_date=date(2026, 1, 1),
        annual_rate_pct=Decimal("30"),
        through=date(2026, 1, 15),
    )
    assert result == Decimal("120.97")


# ---------------------------------------------------------------------- #
# DB-integration (end-to-end through post_depreciation)                  #
# ---------------------------------------------------------------------- #


async def _ctx() -> dict[str, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        async def _by_code(code: str) -> uuid.UUID:
            acct = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id,
                        Account.code == code,
                    )
                )
            ).scalar_one()
            return acct.id

        return {
            "company_id": company.id,
            "cost": await _by_code("1-3310"),
            "accum": await _by_code("1-3320"),
            "dep": await _by_code("6-1500"),
        }


async def test_dv_seed_rows_resolve_via_service() -> None:
    """DV slug ``asset_dv_30`` routes through the service and computes."""
    ctx = await _ctx()
    async with AsyncSessionLocal() as session:
        asset = await svc.create(
            session,
            ctx["company_id"],
            name="DV test — 30% laptop",
            cost_account_id=ctx["cost"],
            accum_dep_account_id=ctx["accum"],
            dep_expense_account_id=ctx["dep"],
            depreciation_model_id="asset_dv_30",
            purchase_date=date(2026, 4, 1),
            in_service_date=date(2026, 4, 1),
            cost=Decimal("10000"),
            code=f"FA-DV-TEST-{uuid.uuid4().hex[:8]}",
        )

    # Service-level cumulative should match the pure function.
    async with AsyncSessionLocal() as session:
        asset = await svc.get(session, asset.id)
        assert asset is not None
        cumulative = await svc.cumulative_depreciation_through(
            session, asset, date(2026, 4, 30)
        )
    assert cumulative == Decimal("250.00")  # 10000 * 0.025 for full April


async def test_dv_post_depreciation_end_to_end() -> None:
    """Full post flow: journal balances, cursor advances, DR/CR line up."""
    ctx = await _ctx()
    async with AsyncSessionLocal() as session:
        asset = await svc.create(
            session,
            ctx["company_id"],
            name="DV end-to-end 40%",
            cost_account_id=ctx["cost"],
            accum_dep_account_id=ctx["accum"],
            dep_expense_account_id=ctx["dep"],
            depreciation_model_id="asset_dv_40",
            purchase_date=date(2026, 4, 1),
            in_service_date=date(2026, 4, 1),
            cost=Decimal("6000"),
            code=f"FA-DV-E2E-{uuid.uuid4().hex[:8]}",
        )

    async with AsyncSessionLocal() as session:
        asset, amount = await svc.post_depreciation(
            session, asset.id, date(2026, 4, 30), posted_by="test"
        )
    # 6000 * 40% / 12 = 200.00 for the first full month.
    assert amount == Decimal("200.00")
    assert asset.last_depreciation_posted_through == date(2026, 4, 30)

    async with AsyncSessionLocal() as session:
        # Find the posted journal + verify it balances.
        result = await session.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == ctx["company_id"],
                JournalEntry.description.like(f"%{asset.code}%"),
                JournalEntry.status == EntryStatus.POSTED,
            )
        )
        entries = result.scalars().all()
        assert len(entries) == 1
        entry = entries[0]
        await session.refresh(entry, ["lines"])
        debits = sum(line.debit for line in entry.lines)
        credits = sum(line.credit for line in entry.lines)
        assert debits == credits == Decimal("200.00")
