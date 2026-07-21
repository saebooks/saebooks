"""Tests for ``saebooks.seed.load_au_coa`` — current/non-current tagging
(M1.5 P1 tail).

The AU CoA seed source carries current/non-current in its Odoo
account_type values; ``_load_accounts`` now surfaces that onto
``Account.balance_sheet_classification`` instead of discarding it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company

pytestmark = pytest.mark.postgres_only


async def _account(company_id, code: str) -> Account:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id, Account.code == code
                )
            )
        ).scalar_one()


async def _seed_company_id():
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def test_current_asset_tagged_current() -> None:
    company_id = await _seed_company_id()
    # 1-1200 Trade Debtors — asset_receivable -> current
    acct = await _account(company_id, "1-1200")
    assert acct.account_type == AccountType.ASSET
    assert acct.balance_sheet_classification == "current"


async def test_fixed_asset_tagged_non_current() -> None:
    company_id = await _seed_company_id()
    # 1-3310 — fixed-asset cost account (asset_fixed) -> non_current
    acct = await _account(company_id, "1-3310")
    assert acct.account_type == AccountType.ASSET
    assert acct.balance_sheet_classification == "non_current"


async def test_current_liability_tagged_current() -> None:
    company_id = await _seed_company_id()
    # 2-1200 Trade Creditors — liability_payable -> current
    acct = await _account(company_id, "2-1200")
    assert acct.account_type == AccountType.LIABILITY
    assert acct.balance_sheet_classification == "current"


async def test_equity_and_income_untagged() -> None:
    company_id = await _seed_company_id()
    gain_acct = await _account(company_id, "4-9100")
    assert gain_acct.account_type == AccountType.OTHER_INCOME
    assert gain_acct.balance_sheet_classification is None


async def test_normal_balance_debit_for_asset() -> None:
    company_id = await _seed_company_id()
    acct = await _account(company_id, "1-1200")
    assert acct.is_contra is False
    assert acct.normal_balance == "debit"


async def test_normal_balance_credit_for_liability() -> None:
    company_id = await _seed_company_id()
    acct = await _account(company_id, "2-1200")
    assert acct.is_contra is False
    assert acct.normal_balance == "credit"


async def test_accum_dep_is_contra_asset_with_credit_normal_balance() -> None:
    company_id = await _seed_company_id()
    # 1-3320 Office Equip Accum Dep — contra-ASSET, credit normal balance.
    acct = await _account(company_id, "1-3320")
    assert acct.account_type == AccountType.ASSET
    assert acct.is_contra is True
    assert acct.normal_balance == "credit"


async def test_equity_subtypes_classified() -> None:
    company_id = await _seed_company_id()
    capital = await _account(company_id, "3-1100")
    drawings = await _account(company_id, "3-1200")
    retained = await _account(company_id, "3-8000")
    current_year = await _account(company_id, "3-9000")
    assert capital.equity_subtype == "share_capital"
    assert drawings.equity_subtype == "drawings"
    assert retained.equity_subtype == "retained_earnings"
    assert current_year.equity_subtype == "retained_earnings"
