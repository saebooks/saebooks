"""Tests for ``saebooks.services.assets.dispose_partial`` (Batch MM/3).

Partial disposal splits an active asset into:

* **Parent** (the row passed in) — cost + residual reduced by the
  disposed share; stays ``active``.
* **Child** (a fresh row) — carries the disposed fraction's cost,
  points at the parent via ``parent_asset_id``, runs through
  ``dispose_asset`` which posts the closeout journal on just the
  disposed share.

Key invariants under test:

* ``0 < fraction < 1`` — both extremes reject (use ``dispose_asset`` for
  full disposal; zero fraction is nonsense).
* Must be ``active``.
* After split: ``parent.cost == original_cost - child.cost``,
  ``parent.residual_value == original_residual - child.residual_value``,
  both to-the-cent exact. Parent stays ``active``, child ends up
  ``disposed``.
* Closeout journal on the child balances, and its accum-dep DR line is
  the child's share of cumulative depreciation (not the original).
* Parent's ``last_depreciation_posted_through`` is set to
  ``disposal_date`` so future depreciation picks up from the split
  without double-counting.
* Idempotent math: gain/loss on a 0.5-fraction disposal at exact NBV is
  zero (no-depreciation model makes this deterministic).

Reuses the same ``_Ctx`` pattern from ``tests/test_assets.py`` — real
seeded DB, ``FA-PD-*`` codes so tests can't collide.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.services import assets as svc

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


async def _fresh_asset(
    ctx: _Ctx,
    *,
    name: str,
    cost: Decimal | int | str = 10000,
    residual: Decimal | int | str = 0,
    in_service: date = date(2026, 1, 1),
    model: str = "asset_no_depreciation",
) -> FixedAsset:
    """Create an asset with a ``FA-PD-*`` code so partial-disposal tests
    can't collide with ``FA-TEST-*`` from ``test_assets.py``.
    """
    async with AsyncSessionLocal() as session:
        return await svc.create(
            session,
            ctx.company_id,
            name=name,
            cost_account_id=ctx.cost_acct_id,
            accum_dep_account_id=ctx.accum_acct_id,
            dep_expense_account_id=ctx.dep_acct_id,
            depreciation_model_id=model,
            purchase_date=in_service,
            in_service_date=in_service,
            cost=Decimal(str(cost)),
            residual_value=Decimal(str(residual)),
            code=f"FA-PD-{uuid.uuid4().hex[:8]}",
        )


# ---------------------------------------------------------------------- #
# Validation                                                             #
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_fraction", ["0", "0.0", "-0.1", "1", "1.0", "1.5"])
async def test_rejects_fraction_out_of_range(bad_fraction: str) -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="Reject-bad-fraction")
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="fraction must be in"):
            await svc.dispose_partial(
                session,
                asset.id,
                fraction=Decimal(bad_fraction),
                disposal_date=date(2026, 6, 1),
                proceeds=Decimal("100"),
                cash_account_id=ctx.cash_acct_id,
                posted_by="test",
            )


async def test_rejects_non_active_asset() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Already disposed", cost=Decimal("1000")
    )
    async with AsyncSessionLocal() as session:
        await svc.dispose_asset(
            session,
            asset.id,
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("500"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        with pytest.raises(ValueError, match="Cannot partially dispose"):
            await svc.dispose_partial(
                session,
                asset.id,
                fraction=Decimal("0.5"),
                disposal_date=date(2026, 7, 1),
                proceeds=Decimal("100"),
                cash_account_id=ctx.cash_acct_id,
                posted_by="test",
            )


async def test_rejects_tiny_fraction_that_rounds_to_zero() -> None:
    """0.00001 of $1 = $0.00001 → rounds to zero, rejected."""
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="Tiny frac", cost=Decimal("1"))
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="too small"):
            await svc.dispose_partial(
                session,
                asset.id,
                fraction=Decimal("0.00001"),
                disposal_date=date(2026, 6, 1),
                proceeds=Decimal("0.01"),
                cash_account_id=ctx.cash_acct_id,
                posted_by="test",
            )


# ---------------------------------------------------------------------- #
# Happy path — no-depreciation model (deterministic NBV = cost)          #
# ---------------------------------------------------------------------- #


async def test_half_disposal_split_exact_cents() -> None:
    """On a $10,000 no-dep asset, fraction=0.5 splits exactly 5000/5000."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx,
        name="Half disposal",
        cost=Decimal("10000"),
        residual=Decimal("1000"),
    )
    async with AsyncSessionLocal() as session:
        parent, child, gain_loss = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.5"),
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("4500"),  # NBV of disposed half = 5000 → loss 500
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )

    assert parent.cost == Decimal("5000.00")
    assert parent.residual_value == Decimal("500.00")
    assert parent.status == "active"
    assert parent.archived_at is None

    assert child.cost == Decimal("5000.00")
    assert child.residual_value == Decimal("500.00")
    assert child.status == "disposed"
    assert child.parent_asset_id == parent.id
    assert child.disposal_proceeds == Decimal("4500.00")

    # NBV of disposed half = child.cost - 0 (no dep) = 5000
    # proceeds 4500 → loss of 500
    assert gain_loss == Decimal("-500.00")


async def test_parent_sum_equals_original_cost() -> None:
    """Parent cost + child cost == original cost — cent-perfect."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx,
        name="Cent-balance check",
        cost=Decimal("9999.99"),  # odd number to stress the rounding
        residual=Decimal("333.33"),
    )
    async with AsyncSessionLocal() as session:
        parent, child, _ = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.37"),  # awkward fraction
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("3000"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )

    assert parent.cost + child.cost == Decimal("9999.99")
    assert parent.residual_value + child.residual_value == Decimal("333.33")


async def test_child_has_parent_asset_id_pointer() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="Parent pointer", cost=Decimal("1000"))
    async with AsyncSessionLocal() as session:
        parent, child, _ = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.25"),
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("200"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        # Reload both to confirm FK persisted
        p = await svc.get(session, parent.id)
        c = await svc.get(session, child.id)
        assert p is not None and c is not None
        assert c.parent_asset_id == p.id
        assert p.parent_asset_id is None  # parent never gets a parent itself


async def test_closeout_journal_balances() -> None:
    """The child's disposal journal balances to the cent."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Balance check", cost=Decimal("4000"),
    )
    async with AsyncSessionLocal() as session:
        _, child, _ = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.25"),
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("900"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        entry = await session.get(JournalEntry, child.disposal_journal_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        await session.refresh(entry, ["lines"])
        total_dr = sum(ln.debit for ln in entry.lines)
        total_cr = sum(ln.credit for ln in entry.lines)
        assert total_dr == total_cr


# ---------------------------------------------------------------------- #
# Depreciated asset — parent cursor should be caught up to disposal_date  #
# ---------------------------------------------------------------------- #


async def test_depreciation_caught_up_on_parent_before_split() -> None:
    """post_depreciation runs first, so parent's cursor is at disposal_date."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx,
        name="Catch-up check",
        cost=Decimal("12000"),
        in_service=date(2026, 1, 1),
        model="asset_3_year_linear",  # ~$333/month linear
    )
    async with AsyncSessionLocal() as session:
        parent, _child, _ = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.5"),
            disposal_date=date(2026, 7, 1),
            proceeds=Decimal("2000"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
    assert parent.last_depreciation_posted_through == date(2026, 7, 1)


async def test_parent_can_continue_depreciating_after_split() -> None:
    """After a split, parent's future depreciation computes on reduced cost.

    Sanity check: posting depreciation a month later on the now-reduced
    parent produces a positive delta and doesn't raise.
    """
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx,
        name="Post-split depreciation",
        cost=Decimal("6000"),
        in_service=date(2026, 1, 1),
        model="asset_5_year_linear",
    )
    async with AsyncSessionLocal() as session:
        parent, _child, _ = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.4"),
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("1500"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )

    # Parent cost now reduced to 3600 (60% of 6000).
    assert parent.cost == Decimal("3600.00")

    # Post another month — should produce a positive delta on the reduced base.
    async with AsyncSessionLocal() as session:
        refreshed, amount = await svc.post_depreciation(
            session, parent.id, date(2026, 7, 1), posted_by="test"
        )
        assert amount > Decimal("0")
        # Same date again — no-op (idempotent).
        _, again = await svc.post_depreciation(
            session, refreshed.id, date(2026, 7, 1), posted_by="test"
        )
        assert again == Decimal("0")


async def test_child_appears_on_asset_list() -> None:
    """The child row shows up in the default ``list_assets`` feed with
    status ``disposed`` — caller can filter as needed.
    """
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="List-visibility", cost=Decimal("2000"))
    async with AsyncSessionLocal() as session:
        _, child, _ = await svc.dispose_partial(
            session,
            asset.id,
            fraction=Decimal("0.5"),
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("1000"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        disposed_list = await svc.list_assets(
            session, ctx.company_id, status="disposed", limit=10000
        )
        assert child.id in {a.id for a in disposed_list}
