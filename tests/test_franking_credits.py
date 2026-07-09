"""Tests for franking credit modelling (gap PRTR-4).

Covers:
* DB: franking_credit_amount column exists on journal_lines.
* DB: franking_credit_amount + franking_percentage exist on invoice_lines.
* DB: total_franking_credits column exists on trust_distributions.
* DB: franking_credit_amount column exists on beneficiary_entitlements.
* Service: create distribution with franking credits splits credits by %.
* Service: grossed_up_income = total_amount + total_franking_credits.
* Service: grossed_up_entitlement = amount + franking_credit_amount per bene.
* Service: zero franking credits (default) — existing behaviour unaffected.
* Journal: franking_credit_amount field is writable on a JournalLine.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.distribution import (
    TrustDistribution,
)
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.services import distributions as svc

pytestmark = pytest.mark.postgres_only

TEST_DATE = date(2099, 6, 30)
TEST_YEAR = 2099


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, equity_account_id, liability_account_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        equity_acct = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.EQUITY,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert equity_acct is not None

        liab_acct = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.LIABILITY,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert liab_acct is not None

        return company.id, equity_acct.id, liab_acct.id


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_lines_has_franking_column() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'journal_lines' "
                "AND column_name = 'franking_credit_amount'"
            )
        )
    assert result.first() is not None, "franking_credit_amount missing from journal_lines"


@pytest.mark.asyncio
async def test_invoice_lines_has_franking_columns() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'invoice_lines' "
                "AND column_name IN ('franking_credit_amount', 'franking_percentage')"
            )
        )
        cols = {row[0] for row in result.all()}
    assert "franking_credit_amount" in cols, "franking_credit_amount missing from invoice_lines"
    assert "franking_percentage" in cols, "franking_percentage missing from invoice_lines"


@pytest.mark.asyncio
async def test_trust_distributions_has_franking_column() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'trust_distributions' "
                "AND column_name = 'total_franking_credits'"
            )
        )
    assert result.first() is not None, "total_franking_credits missing from trust_distributions"


@pytest.mark.asyncio
async def test_beneficiary_entitlements_has_franking_column() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'beneficiary_entitlements' "
                "AND column_name = 'franking_credit_amount'"
            )
        )
    assert result.first() is not None, "franking_credit_amount missing from beneficiary_entitlements"


# ---------------------------------------------------------------------------
# Service — franking credit distribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grossed_up_income_calculation() -> None:
    """$7,000 cash + $3,000 franking = $10,000 grossed-up distributable income."""
    company_id, _equity_id, liab_id = await _ctx()

    async with AsyncSessionLocal() as session:
        dist = await svc.create(
            session,
            company_id,
            financial_year=TEST_YEAR,
            distribution_date=TEST_DATE,
            total_amount=Decimal("7000.00"),
            total_franking_credits=Decimal("3000.00"),
            notes=None,
            entitlements=[
                {
                    "beneficiary_name": "Alice",
                    "percentage": "100",
                    "amount": "7000.00",
                    "account_id": str(liab_id),
                },
            ],
        )

    # Re-fetch inside a session to check eager-loaded entitlements.
    dist_id = dist.id
    async with AsyncSessionLocal() as session:
        dist = await svc.get(session, dist_id)
        assert dist is not None
        assert dist.total_amount == Decimal("7000.00")
        assert dist.total_franking_credits == Decimal("3000.00")
        assert dist.grossed_up_income == Decimal("10000.00")

    # Cleanup
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist_id)
        if raw:
            await session.delete(raw)
            await session.commit()


@pytest.mark.asyncio
async def test_franking_credits_split_by_percentage() -> None:
    """Two beneficiaries — 60/40 split, franking credits allocated proportionally."""
    company_id, _equity_id, liab_id = await _ctx()

    async with AsyncSessionLocal() as session:
        dist = await svc.create(
            session,
            company_id,
            financial_year=TEST_YEAR,
            distribution_date=TEST_DATE,
            total_amount=Decimal("7000.00"),
            total_franking_credits=Decimal("3000.00"),
            notes=None,
            entitlements=[
                {
                    "beneficiary_name": "Alice",
                    "percentage": "60",
                    "amount": "4200.00",
                    "account_id": str(liab_id),
                },
                {
                    "beneficiary_name": "Bob",
                    "percentage": "40",
                    "amount": "2800.00",
                    "account_id": str(liab_id),
                },
            ],
        )

    dist_id = dist.id
    async with AsyncSessionLocal() as session:
        dist = await svc.get(session, dist_id)
        assert dist is not None
        alice = dist.entitlements[0]
        bob = dist.entitlements[1]

        # Alice: 60% of $3000 = $1800
        assert alice.franking_credit_amount == Decimal("1800.00")
        assert alice.grossed_up_entitlement == Decimal("6000.00")  # 4200 + 1800

        # Bob: 40% of $3000 = $1200
        assert bob.franking_credit_amount == Decimal("1200.00")
        assert bob.grossed_up_entitlement == Decimal("4000.00")   # 2800 + 1200

    # Cleanup
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist_id)
        if raw:
            await session.delete(raw)
            await session.commit()


@pytest.mark.asyncio
async def test_explicit_franking_credit_per_entitlement() -> None:
    """Caller can supply franking_credit_amount explicitly per entitlement."""
    company_id, _equity_id, liab_id = await _ctx()

    async with AsyncSessionLocal() as session:
        dist = await svc.create(
            session,
            company_id,
            financial_year=TEST_YEAR,
            distribution_date=TEST_DATE,
            total_amount=Decimal("7000.00"),
            total_franking_credits=Decimal("3000.00"),
            notes=None,
            entitlements=[
                {
                    "beneficiary_name": "Carol",
                    "percentage": "100",
                    "amount": "7000.00",
                    "account_id": str(liab_id),
                    "franking_credit_amount": "3000.00",
                },
            ],
        )

    dist_id = dist.id
    async with AsyncSessionLocal() as session:
        dist = await svc.get(session, dist_id)
        assert dist is not None
        assert dist.entitlements[0].franking_credit_amount == Decimal("3000.00")

    # Cleanup
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist_id)
        if raw:
            await session.delete(raw)
            await session.commit()


@pytest.mark.asyncio
async def test_zero_franking_credits_default_behaviour() -> None:
    """Distributions without franking credits work exactly as before."""
    company_id, _equity_id, liab_id = await _ctx()

    async with AsyncSessionLocal() as session:
        dist = await svc.create(
            session,
            company_id,
            financial_year=TEST_YEAR,
            distribution_date=TEST_DATE,
            total_amount=Decimal("5000.00"),
            notes=None,
            entitlements=[
                {
                    "beneficiary_name": "Dave",
                    "percentage": "100",
                    "amount": "5000.00",
                    "account_id": str(liab_id),
                },
            ],
        )

    dist_id = dist.id
    async with AsyncSessionLocal() as session:
        dist = await svc.get(session, dist_id)
        assert dist is not None
        assert dist.total_franking_credits == Decimal("0")
        assert dist.grossed_up_income == Decimal("5000.00")
        assert dist.entitlements[0].franking_credit_amount == Decimal("0")
        assert dist.entitlements[0].grossed_up_entitlement == Decimal("5000.00")

    # Cleanup
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist_id)
        if raw:
            await session.delete(raw)
            await session.commit()


# ---------------------------------------------------------------------------
# Journal line — franking annotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_line_franking_credit_annotation() -> None:
    """A dividend income journal line can carry a franking_credit_amount."""
    company_id, _, _ = await _ctx()

    async with AsyncSessionLocal() as session:
        income_acct = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company_id,
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert income_acct is not None

        asset_acct = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company_id,
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert asset_acct is not None

        # Post a balanced JE: Dr Bank $7000 / Cr Dividend Income $7000
        # with franking annotation on the income credit line.
        from saebooks.services import journal as journal_svc
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2099, 7, 1),
            description="Fully-franked dividend",
            lines=[
                {
                    "account_id": asset_acct.id,
                    "description": "Bank receipt",
                    "debit": Decimal("7000.00"),
                    "credit": Decimal("0"),
                },
                {
                    "account_id": income_acct.id,
                    "description": "Dividend income (grossed-up via franking annotation)",
                    "debit": Decimal("0"),
                    "credit": Decimal("7000.00"),
                },
            ],
        )
        entry_id = entry.id

        # Write the franking annotation directly via ORM.
        income_line = next(ln for ln in entry.lines if ln.credit > 0)
        income_line.franking_credit_amount = Decimal("3000.00")
        await session.commit()

    # Re-fetch and verify the annotation survived the round-trip.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(JournalLine)
            .where(
                JournalLine.entry_id == entry_id,
                JournalLine.credit > 0,
            )
        )
        line = result.scalars().first()
        assert line is not None
        assert line.franking_credit_amount == Decimal("3000.00")

        # Cleanup
        entry_row = await session.get(JournalEntry, entry_id)
        if entry_row:
            await session.delete(entry_row)
            await session.commit()
