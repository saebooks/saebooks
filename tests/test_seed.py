from sqlalchemy import func, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType


async def test_au_coa_loaded() -> None:
    async with AsyncSessionLocal() as session:
        total = await session.execute(select(func.count()).select_from(Account))
        assert total.scalar_one() == 124

        by_type = await session.execute(
            select(Account.account_type, func.count()).group_by(Account.account_type)
        )
        counts = {t: n for t, n in by_type.all()}
        assert counts[AccountType.ASSET] == 36
        assert counts[AccountType.LIABILITY] == 26
        assert counts[AccountType.EQUITY] == 5
        assert counts[AccountType.INCOME] == 7
        assert counts[AccountType.OTHER_INCOME] == 4
        assert counts[AccountType.EXPENSE] == 37
        assert counts[AccountType.COST_OF_SALES] == 9


async def test_au_coa_reconcile_flag_preserved() -> None:
    async with AsyncSessionLocal() as session:
        row = await session.execute(select(Account).where(Account.code == "11180"))
        account = row.scalar_one()
        assert account.reconcile is True

        row = await session.execute(select(Account).where(Account.code == "11110"))
        account = row.scalar_one()
        assert account.reconcile is False
