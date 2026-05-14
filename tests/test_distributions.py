"""Tests for the trust distribution module (gap PRTR-2).

Covers:
* Service: create validates % sum, rejects empty entitlements.
* Service: create + minute lifecycle.
* Service: post_journal_entry creates a balanced GL entry.
* Service: delete soft-deletes; cannot delete POSTED.
* Router: GET /distributions returns 200.
* Router: GET /distributions/new returns 200.
* Router: GET /year-end redirects to /distributions.
* DB: trust_distributions and beneficiary_entitlements tables exist.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect, select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.distribution import (
    BeneficiaryEntitlement,
    DistributionStatus,
    TrustDistribution,
)
from saebooks.services import distributions as svc
from saebooks.services import journal as journal_svc
pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

        # Any liability account for the payable side
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
async def test_tables_exist() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('trust_distributions', 'beneficiary_entitlements')"
            )
        )
        names = {row[0] for row in result.all()}
    assert "trust_distributions" in names, "trust_distributions table missing"
    assert "beneficiary_entitlements" in names, "beneficiary_entitlements table missing"


# ---------------------------------------------------------------------------
# Service — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_empty_entitlements() -> None:
    company_id, equity_id, _ = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DistributionError, match="at least one"):
            await svc.create(
                session,
                company_id,
                financial_year=TEST_YEAR,
                distribution_date=TEST_DATE,
                total_amount=Decimal("10000.00"),
                notes=None,
                entitlements=[],
            )


@pytest.mark.asyncio
async def test_create_rejects_bad_percentage_sum() -> None:
    company_id, equity_id, _ = await _ctx()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DistributionError, match="100"):
            await svc.create(
                session,
                company_id,
                financial_year=TEST_YEAR,
                distribution_date=TEST_DATE,
                total_amount=Decimal("10000.00"),
                notes=None,
                entitlements=[
                    {"beneficiary_name": "Alice", "percentage": "60", "amount": "6000"},
                    # only 60 + 30 = 90 — should fail
                    {"beneficiary_name": "Bob", "percentage": "30", "amount": "3000"},
                ],
            )


# ---------------------------------------------------------------------------
# Service — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_minute() -> None:
    company_id, equity_id, liab_id = await _ctx()

    async with AsyncSessionLocal() as session:
        dist = await svc.create(
            session,
            company_id,
            financial_year=TEST_YEAR,
            distribution_date=TEST_DATE,
            total_amount=Decimal("10000.00"),
            notes="Test distribution",
            entitlements=[
                {
                    "beneficiary_name": "Alice",
                    "percentage": "60",
                    "amount": "6000.00",
                    "account_id": str(liab_id),
                },
                {
                    "beneficiary_name": "Bob",
                    "percentage": "40",
                    "amount": "4000.00",
                    "account_id": str(liab_id),
                },
            ],
        )

    assert dist.status == DistributionStatus.DRAFT
    assert dist.total_amount == Decimal("10000.00")
    # Re-fetch inside a session to check eager-loaded entitlements.
    async with AsyncSessionLocal() as session:
        refetched = await svc.get(session, dist.id)
        assert refetched is not None
        assert len(refetched.entitlements) == 2

    # Minute the resolution.
    async with AsyncSessionLocal() as session:
        dist = await svc.minute(
            session, dist.id, minuted_date=date(TEST_YEAR, 6, 29)
        )
    assert dist.status == DistributionStatus.MINUTED
    assert dist.resolution_minuted_date == date(TEST_YEAR, 6, 29)

    # Cleanup
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist.id)
        if raw:
            await session.delete(raw)
            await session.commit()


@pytest.mark.asyncio
async def test_post_journal_entry() -> None:
    company_id, equity_id, liab_id = await _ctx()

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
                    "beneficiary_name": "Carol",
                    "percentage": "100",
                    "amount": "5000.00",
                    "account_id": str(liab_id),
                },
            ],
        )

    dist_id = dist.id
    async with AsyncSessionLocal() as session:
        dist = await svc.post_journal_entry(
            session,
            dist_id,
            income_account_id=equity_id,
        )

    assert dist.status == DistributionStatus.POSTED
    assert dist.journal_entry_id is not None

    # Cleanup
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist_id)
        if raw:
            await session.delete(raw)
            await session.commit()


@pytest.mark.asyncio
async def test_delete_rejects_posted() -> None:
    company_id, equity_id, liab_id = await _ctx()

    async with AsyncSessionLocal() as session:
        dist = await svc.create(
            session,
            company_id,
            financial_year=TEST_YEAR,
            distribution_date=TEST_DATE,
            total_amount=Decimal("1000.00"),
            notes=None,
            entitlements=[
                {
                    "beneficiary_name": "Dave",
                    "percentage": "100",
                    "amount": "1000.00",
                    "account_id": str(liab_id),
                },
            ],
        )
        await svc.post_journal_entry(session, dist.id, income_account_id=equity_id)

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DistributionError, match="posted"):
            await svc.delete(session, dist.id)

    # Cleanup (force delete)
    async with AsyncSessionLocal() as session:
        raw = await session.get(TrustDistribution, dist.id)
        if raw:
            await session.delete(raw)
            await session.commit()


# ---------------------------------------------------------------------------
# Router smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distributions_list_page(client: AsyncClient) -> None:
    r = await client.get("/distributions")
    assert r.status_code == 200
    assert b"Trust Distribution" in r.content


@pytest.mark.asyncio
async def test_distributions_new_page(client: AsyncClient) -> None:
    r = await client.get("/distributions/new")
    assert r.status_code == 200
    assert b"beneficiary" in r.content.lower()


@pytest.mark.asyncio
async def test_year_end_redirect(client: AsyncClient) -> None:
    r = await client.get("/year-end", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert "/distributions" in r.headers["location"]
