from sqlalchemy import func, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType


async def test_au_coa_loaded() -> None:
    """Seeded AU CoA row counts — the exact values grow with the seed over time.

    When the seed is extended (eg. fixed-asset register adding Gain/Loss on
    Disposal rows) update the expected counts here. Kept strict so a
    silently-dropped account type fails loud.
    """
    async with AsyncSessionLocal() as session:
        total = await session.execute(select(func.count()).select_from(Account))
        assert total.scalar_one() == 135

        by_type = await session.execute(
            select(Account.account_type, func.count()).group_by(Account.account_type)
        )
        counts = {t: n for t, n in by_type.all()}
        assert counts[AccountType.ASSET] == 42
        assert counts[AccountType.LIABILITY] == 29
        assert counts[AccountType.EQUITY] == 6
        assert counts[AccountType.INCOME] == 5
        assert counts[AccountType.OTHER_INCOME] == 6
        assert counts[AccountType.EXPENSE] == 40
        assert counts[AccountType.COST_OF_SALES] == 7


async def test_au_coa_reconcile_flag_preserved() -> None:
    async with AsyncSessionLocal() as session:
        # Codes are stored hyphenated (`1-1180` not `11180`) — see
        # migration 0010_hyphenated_account_codes.
        row = await session.execute(select(Account).where(Account.code == "1-1180"))
        account = row.scalar_one()
        assert account.reconcile is True

        row = await session.execute(select(Account).where(Account.code == "1-1110"))
        account = row.scalar_one()
        assert account.reconcile is False
