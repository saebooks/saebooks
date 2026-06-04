"""Tests for AU CoA seed integrity.

These tests verify that specific known accounts from the AU seed CSV are
present and have the correct attributes. Exact counts are NOT asserted
because the seed company accumulates additional accounts over time (tests,
real usage, QBO imports) and the counts would be unreliable on a persistent
shared DB.
"""
import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.services.companies import ensure_seed_company

pytestmark = pytest.mark.postgres_only


async def _seed_company_id() -> object:
    """Return the ID of the seed company (identified by SEED_COMPANY_NAME env)."""
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        return company.id


async def test_au_coa_loaded() -> None:
    """Specific AU CoA accounts are present in the seed company.

    Verifies a cross-section of accounts from each type using codes that
    come from the AU seed CSV (load_au_coa.py). Presence checks rather
    than exact counts so a polluted shared DB doesn't break the test.
    """
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        # Sample one account from each type to confirm the seed ran.
        # Codes are the hyphenated form stored after migration 0010.
        expected = {
            "1-1110": AccountType.ASSET,          # Bank
            "1-1180": AccountType.ASSET,           # Undeposited Funds
            "2-1110": AccountType.LIABILITY,       # Credit Card
            "3-8000": AccountType.EQUITY,          # Retained Earnings
            "4-3000": AccountType.INCOME,          # Consignment Sales
            "4-5000": AccountType.OTHER_INCOME,    # Late Fees Collected
            "5-2000": AccountType.COST_OF_SALES,   # Wholesale Cost of Sales
            "6-2000": AccountType.EXPENSE,         # Late Fees Paid
            "6-2510": AccountType.EXPENSE,         # Cash Short and Over
        }
        for code, expected_type in expected.items():
            row = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company_id,
                        Account.code == code,
                    )
                )
            ).scalar_one_or_none()
            assert row is not None, f"Seed account {code!r} missing from company"
            assert row.account_type == expected_type, (
                f"Account {code!r}: expected type {expected_type}, got {row.account_type}"
            )


async def test_smsf_super_accounts_present() -> None:
    """6-2420-SG and 6-2420-SMSF must both exist as EXPENSE accounts.

    Contractor self-directed super (SMSF) must be distinguishable from
    employer SG in the GL for correct tax attribution.
    """
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        for code in ("6-2420-SG", "6-2420-SMSF"):
            row = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company_id,
                        Account.code == code,
                    )
                )
            ).scalar_one_or_none()
            assert row is not None, f"SMSF super account {code!r} missing from seed"
            assert row.account_type == AccountType.EXPENSE, (
                f"Account {code!r}: expected EXPENSE, got {row.account_type}"
            )
            assert row.is_header is False, f"Account {code!r} must be postable (is_header=False)"


async def test_au_coa_reconcile_flag_preserved() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        # Codes are stored hyphenated (`1-1180` not `11180`) — see
        # migration 0010_hyphenated_account_codes.
        row = await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == "1-1180",
            )
        )
        account = row.scalar_one()
        assert account.reconcile is True

        row = await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == "1-1110",
            )
        )
        account = row.scalar_one()
        assert account.reconcile is False
