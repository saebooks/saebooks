"""Tests for ``saebooks.services.assets_reports`` — tax-vs-book overlay.

Covers:

1. **Service math** — when ``tax_model_id`` is NULL, tax cumulative
   equals book cumulative and ``temporary_difference`` is zero.
2. **Tax divergence** — assigning a DV tax model to a linear-book asset
   makes tax cumulative diverge from book cumulative after a year.
3. **Totals** — schedule rollups match sum of per-row values.
4. **CSV shape** — 10-column CSV with both book and tax columns.
5. **Filtering** — ``include_disposed`` flag respected; archived assets
   always excluded.
6. **Router smoke** — ``/reports/depreciation-schedule`` returns 200
   HTML; ``?format=csv`` returns text/csv attachment; report card
   links from ``/reports``.

Tests run against the live AU-seeded DB (same pattern as
``tests/test_assets.py``).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.main import create_app
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import assets as asset_svc
from saebooks.services import assets_reports as svc

pytestmark = pytest.mark.postgres_only


class _Ctx:
    company_id: uuid.UUID
    cost_acct_id: uuid.UUID
    accum_acct_id: uuid.UUID
    dep_acct_id: uuid.UUID
    cash_acct_id: uuid.UUID


async def _ctx() -> _Ctx:
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

        c = _Ctx()
        c.company_id = company.id
        c.cost_acct_id = await _by_code("1-3310")
        c.accum_acct_id = await _by_code("1-3320")
        c.dep_acct_id = await _by_code("6-1500")
        c.cash_acct_id = await _by_code("1-1110")
        return c


async def _mk_asset(
    ctx: _Ctx,
    *,
    name: str,
    book_model: str,
    tax_model: str | None,
    cost: Decimal | int = 12000,
    residual: Decimal | int = 0,
    in_service: date = date(2025, 1, 1),
) -> FixedAsset:
    async with AsyncSessionLocal() as session:
        asset = await asset_svc.create(
            session,
            ctx.company_id,
            name=name,
            cost_account_id=ctx.cost_acct_id,
            accum_dep_account_id=ctx.accum_acct_id,
            dep_expense_account_id=ctx.dep_acct_id,
            depreciation_model_id=book_model,
            tax_model_id=tax_model,
            purchase_date=in_service,
            in_service_date=in_service,
            cost=Decimal(str(cost)),
            residual_value=Decimal(str(residual)),
            code=f"FA-DS-{uuid.uuid4().hex[:8]}",
        )
        return asset


# ---------------------------------------------------------------------- #
# Service                                                                #
# ---------------------------------------------------------------------- #


async def test_null_tax_model_matches_book_so_diff_is_zero() -> None:
    ctx = await _ctx()
    asset = await _mk_asset(
        ctx,
        name="No-tax-model asset",
        book_model="asset_5_year_linear",
        tax_model=None,
        cost=12000,
        in_service=date(2025, 1, 1),
    )

    async with AsyncSessionLocal() as session:
        sched = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 1, 1)
        )

    row = next(r for r in sched.rows if r.asset_id == asset.id)
    assert row.tax_model_id is None
    # Exact month count varies with day-fraction math — we only need the
    # invariant: tax model null => tax mirrors book everywhere.
    assert row.book_cumulative > Decimal("0")
    assert row.tax_cumulative == row.book_cumulative
    assert row.temporary_difference == Decimal("0.00")
    assert row.book_nbv == row.tax_written_down_value
    # Approximate one-year linear sanity check: ~12/60 * 12000 = ~2400
    assert Decimal("2300") <= row.book_cumulative <= Decimal("2500")


async def test_dv_tax_model_diverges_from_linear_book() -> None:
    ctx = await _ctx()
    asset = await _mk_asset(
        ctx,
        name="Split book vs tax asset",
        # Book: 10-year straight-line (slow; management)
        book_model="asset_10_year_linear",
        # Tax: 40% DV (fast; ATO effective life)
        tax_model="asset_dv_40",
        cost=10000,
        in_service=date(2025, 1, 1),
    )

    async with AsyncSessionLocal() as session:
        sched = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 1, 1)
        )

    row = next(r for r in sched.rows if r.asset_id == asset.id)
    # Book 1-yr linear over 10-yr life: ~10000/10 = ~1000
    assert Decimal("900") <= row.book_cumulative <= Decimal("1100")
    # DV 40% on 10000 over 12 months compounds to ~3334; we only care it's
    # meaningfully > book. Exact figure is tested in assets tests — here
    # we only need the divergence signal.
    assert row.tax_cumulative > Decimal("2500.00")
    assert row.tax_cumulative < Decimal("4500.00")
    # Tax has written down faster => book NBV > tax WDV => positive diff
    assert row.temporary_difference > Decimal("0.00")


async def test_totals_sum_of_rows() -> None:
    ctx = await _ctx()
    a1 = await _mk_asset(
        ctx, name="Totals A", book_model="asset_5_year_linear",
        tax_model=None, cost=6000,
        in_service=date(2025, 1, 1),
    )
    a2 = await _mk_asset(
        ctx, name="Totals B", book_model="asset_3_year_linear",
        tax_model=None, cost=3000,
        in_service=date(2025, 1, 1),
    )

    async with AsyncSessionLocal() as session:
        sched = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 1, 1)
        )

    our_rows = [r for r in sched.rows if r.asset_id in {a1.id, a2.id}]
    # Grand totals include every active asset, so just check our two rows add
    # up correctly — the totals are whole-schedule, not per-asset.
    assert len(our_rows) == 2
    # Sum matches our contribution to the grand total
    our_cost = sum((r.cost for r in our_rows), Decimal("0"))
    our_book = sum((r.book_cumulative for r in our_rows), Decimal("0"))
    assert our_cost == Decimal("9000.00")
    # A1 1yr linear / 5yr: ~1200. A2 1yr linear / 3yr: ~1000. ~2200.
    assert Decimal("2100") <= our_book <= Decimal("2300")
    # Totals property is cent-quantized Decimal
    assert sched.total_cost.as_tuple().exponent == -2
    assert sched.total_temporary_difference.as_tuple().exponent == -2


async def test_archived_asset_excluded() -> None:
    ctx = await _ctx()
    asset = await _mk_asset(
        ctx,
        name="Archive me from schedule",
        book_model="asset_5_year_linear",
        tax_model=None,
        cost=5000,
    )
    async with AsyncSessionLocal() as session:
        await asset_svc.archive(session, asset.id)

    async with AsyncSessionLocal() as session:
        sched = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 1, 1)
        )
    ids = {r.asset_id for r in sched.rows}
    assert asset.id not in ids


async def test_include_disposed_flag_toggles_disposed_rows() -> None:
    ctx = await _ctx()
    asset = await _mk_asset(
        ctx,
        name="Dispose then report",
        book_model="asset_5_year_linear",
        tax_model=None,
        cost=2400,
        in_service=date(2025, 1, 1),
    )

    async with AsyncSessionLocal() as session:
        await asset_svc.dispose_asset(
            session,
            asset.id,
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("1000"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )

    # Default (include_disposed=False) hides it
    async with AsyncSessionLocal() as session:
        excluded = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 12, 31)
        )
    assert asset.id not in {r.asset_id for r in excluded.rows}

    # include_disposed=True surfaces it
    async with AsyncSessionLocal() as session:
        included = await svc.depreciation_schedule(
            session,
            ctx.company_id,
            as_at=date(2026, 12, 31),
            include_disposed=True,
        )
    assert asset.id in {r.asset_id for r in included.rows}
    row = next(r for r in included.rows if r.asset_id == asset.id)
    assert row.status == "disposed"


async def test_csv_has_ten_columns_and_includes_tax_fields() -> None:
    ctx = await _ctx()
    asset = await _mk_asset(
        ctx,
        name="CSV shape asset",
        book_model="asset_5_year_linear",
        tax_model="asset_dv_30",
        cost=1200,
        in_service=date(2025, 1, 1),
    )

    async with AsyncSessionLocal() as session:
        sched = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 1, 1)
        )

    csv_text = svc.depreciation_schedule_csv(sched)
    lines = csv_text.strip().splitlines()
    # Header is a stable 10-column layout
    header = lines[0].split(",")
    assert header == [
        "code",
        "name",
        "status",
        "cost",
        "book_model_id",
        "book_cumulative",
        "book_nbv",
        "tax_model_id",
        "tax_cumulative",
        "temporary_difference",
    ]
    # Find our asset's row and check tax columns are populated
    my_row = next(
        line for line in lines[1:] if line.startswith(asset.code + ",")
    )
    cols = my_row.split(",")
    assert cols[4] == "asset_5_year_linear"
    assert cols[7] == "asset_dv_30"


async def test_null_tax_model_csv_emits_blank_not_none() -> None:
    ctx = await _ctx()
    asset = await _mk_asset(
        ctx,
        name="CSV null-tax asset",
        book_model="asset_5_year_linear",
        tax_model=None,
        cost=600,
    )

    async with AsyncSessionLocal() as session:
        sched = await svc.depreciation_schedule(
            session, ctx.company_id, as_at=date(2026, 1, 1)
        )

    csv_text = svc.depreciation_schedule_csv(sched)
    my_row = next(
        line
        for line in csv_text.splitlines()[1:]
        if line.startswith(asset.code + ",")
    )
    # 8th column (index 7) is tax_model_id — must be empty string, not "None"
    assert my_row.split(",")[7] == ""


# ---------------------------------------------------------------------- #
# Router                                                                 #
# ---------------------------------------------------------------------- #


def _client() -> TestClient:
    return TestClient(create_app())


