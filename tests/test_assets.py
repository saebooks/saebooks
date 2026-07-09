"""Tests for ``saebooks.services.assets``.

Covers:

1. **CRUD** — create auto-codes, update locks out cost/residual once
   depreciation has posted, archive is a soft-delete.
2. **Depreciation math** — linear over exact useful life, ``no_depreciation``
   always returns zero, cap at depreciable base, idempotency.
3. **Post depreciation** — journal actually posts, DR/CR hit the right
   accounts, cursor advances.
4. **Disposal** — gain path, loss path, exactly-at-NBV, no-dep asset
   disposed with proceeds, journal balances.

Tests run against the live AU-seeded DB (same pattern as
``tests/test_journal.py``): fetch the seed company + real accounts,
create assets with ``FA-TEST-*`` codes so multiple runs don't collide.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.journal import EntryStatus, JournalEntry, JournalOrigin
from saebooks.services import assets as svc

pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------- #
# Fixtures — reach into the real seeded DB                               #
# ---------------------------------------------------------------------- #


class _Ctx:
    company_id: uuid.UUID
    cost_acct_id: uuid.UUID
    accum_acct_id: uuid.UUID
    dep_acct_id: uuid.UUID
    cash_acct_id: uuid.UUID


async def _ctx() -> _Ctx:
    """Grab the company + the five GL accounts every test needs."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
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
    cost: Decimal | int | str = 12000,
    residual: Decimal | int | str = 0,
    in_service: date = date(2026, 1, 1),
    model: str = "asset_5_year_linear",
) -> FixedAsset:
    """Create a fresh asset; guarantee unique code via UUID suffix."""
    async with AsyncSessionLocal() as session:
        asset = await svc.create(
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
            code=f"FA-TEST-{uuid.uuid4().hex[:8]}",
        )
        return asset


# ---------------------------------------------------------------------- #
# CRUD                                                                   #
# ---------------------------------------------------------------------- #


async def test_create_auto_code_and_defaults() -> None:
    ctx = await _ctx()
    async with AsyncSessionLocal() as session:
        asset = await svc.create(
            session,
            ctx.company_id,
            name="Laptop",
            cost_account_id=ctx.cost_acct_id,
            accum_dep_account_id=ctx.accum_acct_id,
            dep_expense_account_id=ctx.dep_acct_id,
            depreciation_model_id="asset_3_year_linear",
            purchase_date=date(2026, 4, 1),
            cost=Decimal("3000"),
        )
        assert asset.code.startswith("FA-")
        # in_service_date defaults to purchase_date
        assert asset.in_service_date == date(2026, 4, 1)
        # residual_value defaults to 0
        assert asset.residual_value == Decimal("0.00")
        assert asset.status == "active"
        assert asset.last_depreciation_posted_through is None


async def test_update_locks_money_fields_once_depreciated() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="Update-lock asset")

    async with AsyncSessionLocal() as session:
        # Post one month of depreciation (after seeded period-lock 2026-03-31).
        await svc.post_depreciation(
            session, asset.id, date(2026, 5, 1), posted_by="test"
        )

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="Cannot edit 'cost'"):
            await svc.update(session, asset.id, cost=Decimal("999"))
        # Non-money field still allowed
        updated = await svc.update(session, asset.id, location="Desk 1")
        assert updated.location == "Desk 1"


async def test_archive_soft_deletes() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="Archive me")
    async with AsyncSessionLocal() as session:
        archived = await svc.archive(session, asset.id)
        assert archived.archived_at is not None
        assert archived.status == "archived"

        listed = await svc.list_assets(session, ctx.company_id)
        assert archived.id not in {a.id for a in listed}


# ---------------------------------------------------------------------- #
# Depreciation math                                                      #
# ---------------------------------------------------------------------- #


async def test_no_depreciation_model_returns_zero() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Land", cost=Decimal("100000"), model="asset_no_depreciation"
    )
    async with AsyncSessionLocal() as session:
        fresh = await svc.get(session, asset.id)
        assert fresh is not None
        amount = await svc.cumulative_depreciation_through(
            session, fresh, date(2036, 1, 1)
        )
        assert amount == Decimal("0")


async def test_linear_full_life_hits_depreciable_base() -> None:
    """5-year linear over exact useful life fully depreciates the base."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx,
        name="5yr full",
        cost=Decimal("6000"),
        residual=Decimal("1000"),
        in_service=date(2026, 1, 1),
        model="asset_5_year_linear",
    )
    async with AsyncSessionLocal() as session:
        fresh = await svc.get(session, asset.id)
        assert fresh is not None
        # 5 years x 365.25 days ≈ 1826 days from 2026-01-01 lands in 2031.
        # Depreciable base is 5000 (6000 - 1000).
        amount = await svc.cumulative_depreciation_through(
            session, fresh, date(2035, 1, 1)
        )
        assert amount == Decimal("5000.00")


async def test_linear_partial_period_prorates() -> None:
    """After exactly one year on a 5-year linear, expect ~20% of base."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx,
        name="5yr partial",
        cost=Decimal("6000"),
        residual=Decimal("0"),
        in_service=date(2026, 1, 1),
        model="asset_5_year_linear",
    )
    async with AsyncSessionLocal() as session:
        fresh = await svc.get(session, asset.id)
        assert fresh is not None
        # One calendar year: (365 + 1) days / (5 * 365.25) days ≈ 0.20041
        # 6000 x 0.20041 ≈ 1202.46
        amount = await svc.cumulative_depreciation_through(
            session, fresh, date(2026, 12, 31)
        )
        # Allow a couple of cents of drift for the 30.4375 approximation.
        assert Decimal("1190") < amount < Decimal("1215")


async def test_compute_idempotent_after_posting() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Idempotency", cost=Decimal("6000"), model="asset_5_year_linear"
    )
    async with AsyncSessionLocal() as session:
        _, first_amount = await svc.post_depreciation(
            session, asset.id, date(2026, 5, 1), posted_by="test"
        )
        assert first_amount > 0
        # Same date again: zero incremental amount, cursor unchanged.
        _, second = await svc.post_depreciation(
            session, asset.id, date(2026, 5, 1), posted_by="test"
        )
        assert second == Decimal("0")


# ---------------------------------------------------------------------- #
# post_depreciation journals                                             #
# ---------------------------------------------------------------------- #


async def test_post_depreciation_creates_balanced_journal() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(ctx, name="Journal test", cost=Decimal("6000"))
    async with AsyncSessionLocal() as session:
        refreshed, amount = await svc.post_depreciation(
            session, asset.id, date(2026, 4, 1), posted_by="test"
        )
        assert amount > 0
        assert refreshed.last_depreciation_posted_through == date(2026, 4, 1)

        entries = (
            await session.execute(
                select(JournalEntry)
                .where(JournalEntry.company_id == ctx.company_id)
                .where(JournalEntry.status == EntryStatus.POSTED)
                .order_by(JournalEntry.created_at.desc())
                .limit(1)
            )
        ).scalars().all()
        assert entries, "depreciation journal should have posted"
        entry = entries[0]
        # JE-provenance: depreciation JE self-declares origin + source asset.
        assert entry.origin == JournalOrigin.DEPRECIATION
        assert entry.source_type == "fixed_asset"
        assert entry.source_id == asset.id
        # Reload lines
        await session.refresh(entry, ["lines"])
        debits = [ln for ln in entry.lines if ln.debit > 0]
        credits = [ln for ln in entry.lines if ln.credit > 0]
        assert len(debits) == 1
        assert len(credits) == 1
        assert debits[0].account_id == ctx.dep_acct_id
        assert credits[0].account_id == ctx.accum_acct_id
        assert debits[0].debit == credits[0].credit == amount


# ---------------------------------------------------------------------- #
# Disposal                                                               #
# ---------------------------------------------------------------------- #


async def test_dispose_with_gain() -> None:
    """Dispose for more than NBV → 4-9100 gets credited for the gain."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Gain sale", cost=Decimal("6000"), model="asset_5_year_linear"
    )
    async with AsyncSessionLocal() as session:
        refreshed, gain_loss = await svc.dispose_asset(
            session,
            asset.id,
            disposal_date=date(2027, 1, 1),
            proceeds=Decimal("7000"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        # Roughly 1 year of 5-year dep posted → NBV ≈ 4800
        # Proceeds 7000 > NBV ⇒ gain in the ballpark of 2200
        assert gain_loss > Decimal("1500")
        assert refreshed.status == "disposed"
        assert refreshed.disposal_journal_id is not None

        entry = await session.get(JournalEntry, refreshed.disposal_journal_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        # JE-provenance: disposal JE self-declares origin + source asset.
        assert entry.origin == JournalOrigin.FIXED_ASSET
        assert entry.source_type == "fixed_asset"
        assert entry.source_id == asset.id
        await session.refresh(entry, ["lines"])
        total_debit = sum(ln.debit for ln in entry.lines)
        total_credit = sum(ln.credit for ln in entry.lines)
        assert total_debit == total_credit


async def test_dispose_with_loss() -> None:
    """Dispose for less than NBV → 6-9100 gets debited for the loss."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Loss sale", cost=Decimal("6000"), model="asset_5_year_linear"
    )
    async with AsyncSessionLocal() as session:
        refreshed, gain_loss = await svc.dispose_asset(
            session,
            asset.id,
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("100"),
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        assert gain_loss < 0
        assert refreshed.status == "disposed"

        entry = await session.get(JournalEntry, refreshed.disposal_journal_id)
        assert entry is not None
        await session.refresh(entry, ["lines"])
        # Loss account should carry the debit plug
        loss_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == ctx.company_id,
                    Account.code == "6-9100",
                )
            )
        ).scalar_one()
        loss_lines = [ln for ln in entry.lines if ln.account_id == loss_acct.id]
        assert len(loss_lines) == 1
        assert loss_lines[0].debit == -gain_loss


async def test_dispose_at_exact_nbv_no_gain_loss_line() -> None:
    """Proceeds == NBV → no gain/loss line, journal still balances."""
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Break-even", cost=Decimal("6000"), model="asset_no_depreciation"
    )
    async with AsyncSessionLocal() as session:
        refreshed, gain_loss = await svc.dispose_asset(
            session,
            asset.id,
            disposal_date=date(2026, 6, 1),
            proceeds=Decimal("6000"),  # no dep on this model → NBV = cost
            cash_account_id=ctx.cash_acct_id,
            posted_by="test",
        )
        assert gain_loss == Decimal("0.00")
        entry = await session.get(JournalEntry, refreshed.disposal_journal_id)
        assert entry is not None
        await session.refresh(entry, ["lines"])
        # Two lines only: DR cash, CR cost
        assert len(entry.lines) == 2


async def test_cannot_dispose_already_disposed() -> None:
    ctx = await _ctx()
    asset = await _fresh_asset(
        ctx, name="Double dispose", cost=Decimal("1000"),
        model="asset_no_depreciation",
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
        with pytest.raises(ValueError, match="Cannot dispose asset"):
            await svc.dispose_asset(
                session,
                asset.id,
                disposal_date=date(2026, 7, 1),
                proceeds=Decimal("100"),
                cash_account_id=ctx.cash_acct_id,
                posted_by="test",
            )


# ---------------------------------------------------------------------- #
# Convert to inventory (MOTR-3)                                          #
# ---------------------------------------------------------------------- #


async def _inventory_acct_id(ctx: _Ctx) -> uuid.UUID:
    """Fetch the Trading Stock on Hand account (1-1330) for tests."""
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == ctx.company_id,
                    Account.code == "1-1330",
                )
            )
        ).scalar_one_or_none()
        # Fall back to any non-header asset account if seed lacks 1-1330.
        if acct is None:
            acct = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == ctx.company_id,
                        Account.is_header.is_(False),
                        Account.archived_at.is_(None),
                        Account.account_type == AccountType.ASSET,
                    ).order_by(Account.code).limit(1)
                )
            ).scalar_one()
        return acct.id


async def test_convert_to_inventory_posts_journal_and_marks_disposed() -> None:
    """Conversion journal balances; asset stamped disposed; proceeds = NBV."""
    ctx = await _ctx()
    inv_id = await _inventory_acct_id(ctx)
    asset = await _fresh_asset(
        ctx,
        name="Demo vehicle MOTR-3",
        cost=Decimal("80000"),
        model="asset_5_year_linear",
        in_service=date(2026, 1, 1),
    )
    async with AsyncSessionLocal() as session:
        refreshed, nbv = await svc.convert_to_inventory(
            session,
            asset.id,
            conversion_date=date(2026, 5, 1),
            inventory_account_id=inv_id,
            posted_by="test",
        )

        assert refreshed.status == "disposed"
        assert refreshed.disposal_date == date(2026, 5, 1)
        assert refreshed.disposal_proceeds == nbv
        assert refreshed.disposal_journal_id is not None
        assert nbv > Decimal("0")  # 4 months of 5-year dep, still has value

        entry = await session.get(JournalEntry, refreshed.disposal_journal_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        await session.refresh(entry, ["lines"])

        total_debit = sum(ln.debit for ln in entry.lines)
        total_credit = sum(ln.credit for ln in entry.lines)
        assert total_debit == total_credit, "Conversion journal must balance"

        # Inventory account must be debited at NBV.
        inv_lines = [ln for ln in entry.lines if ln.account_id == inv_id]
        assert len(inv_lines) == 1
        assert inv_lines[0].debit == nbv

        # Cost account must be credited at full cost.
        cost_lines = [ln for ln in entry.lines if ln.account_id == ctx.cost_acct_id]
        assert len(cost_lines) == 1
        assert cost_lines[0].credit == Decimal("80000.00")


async def test_convert_fully_depreciated_asset() -> None:
    """Fully depreciated asset (NBV=0) converts without an inventory line."""
    ctx = await _ctx()
    inv_id = await _inventory_acct_id(ctx)
    asset = await _fresh_asset(
        ctx,
        name="Fully dep demo MOTR-3",
        cost=Decimal("10000"),
        model="asset_3_year_linear",
        in_service=date(2020, 1, 1),
    )
    async with AsyncSessionLocal() as session:
        refreshed, nbv = await svc.convert_to_inventory(
            session,
            asset.id,
            conversion_date=date(2026, 5, 1),
            inventory_account_id=inv_id,
            posted_by="test",
        )
        assert nbv == Decimal("0.00")
        assert refreshed.status == "disposed"

        entry = await session.get(JournalEntry, refreshed.disposal_journal_id)
        assert entry is not None
        await session.refresh(entry, ["lines"])
        # No inventory line when NBV = 0; journal still balances.
        inv_lines = [ln for ln in entry.lines if ln.account_id == inv_id]
        assert len(inv_lines) == 0
        total_debit = sum(ln.debit for ln in entry.lines)
        total_credit = sum(ln.credit for ln in entry.lines)
        assert total_debit == total_credit


async def test_cannot_convert_already_disposed() -> None:
    ctx = await _ctx()
    inv_id = await _inventory_acct_id(ctx)
    asset = await _fresh_asset(
        ctx, name="Double convert MOTR-3", cost=Decimal("5000"),
        model="asset_no_depreciation",
    )
    async with AsyncSessionLocal() as session:
        await svc.convert_to_inventory(
            session,
            asset.id,
            conversion_date=date(2026, 5, 1),
            inventory_account_id=inv_id,
            posted_by="test",
        )
        with pytest.raises(ValueError, match="Cannot convert asset"):
            await svc.convert_to_inventory(
                session,
                asset.id,
                conversion_date=date(2026, 6, 1),
                inventory_account_id=inv_id,
                posted_by="test",
            )
