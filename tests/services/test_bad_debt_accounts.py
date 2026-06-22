"""Create-on-demand resolvers for the bad-debt accounts (Task 2).

``get_bad_debt_recovery_account`` resolves 4-1290 (OTHER_INCOME, "Bad Debt
Recovery"), creating it if absent and returning the same row on the second
call. ``get_bad_debt_expense_account`` does the same for 6-2050 (EXPENSE,
"Bad Debts") because the AU seed CoA does not ship a bad-debts account.

The resolver flushes but does NOT commit (the posting caller owns the
transaction), so the test commits after the first resolve to persist the row
before re-resolving in a fresh session — mirroring real caller behaviour.
"""
import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import AccountType
from saebooks.services import accounts as accounts_svc

pytestmark = pytest.mark.postgres_only


async def test_recovery_account_created_on_demand(seeded_company):
    cid, _tid, _accts = seeded_company
    async with AsyncSessionLocal() as session:
        acct = await accounts_svc.get_bad_debt_recovery_account(session, cid)
        assert acct.code == "4-1290"
        assert acct.name == "Bad Debt Recovery"
        assert acct.account_type == AccountType.OTHER_INCOME
        assert acct.company_id == cid
        first_id = acct.id
        await session.commit()

    # Second call (fresh session) must return the SAME account, not a dup.
    async with AsyncSessionLocal() as session:
        again = await accounts_svc.get_bad_debt_recovery_account(session, cid)
        assert again.id == first_id


async def test_expense_account_created_on_demand(seeded_company):
    cid, _tid, _accts = seeded_company
    async with AsyncSessionLocal() as session:
        acct = await accounts_svc.get_bad_debt_expense_account(session, cid)
        assert acct.code == "6-2050"
        assert acct.name == "Bad Debts"
        assert acct.account_type == AccountType.EXPENSE
        assert acct.company_id == cid
        first_id = acct.id
        await session.commit()

    async with AsyncSessionLocal() as session:
        again = await accounts_svc.get_bad_debt_expense_account(session, cid)
        assert again.id == first_id


async def test_resolver_is_company_scoped(seeded_company):
    """Re-resolving in a fresh session is idempotent and stays company-scoped."""
    cid, _tid, _accts = seeded_company
    async with AsyncSessionLocal() as session:
        a = await accounts_svc.get_bad_debt_recovery_account(session, cid)
        a_id = a.id
        await session.commit()
    async with AsyncSessionLocal() as session:
        again = await accounts_svc.get_bad_debt_recovery_account(session, cid)
        assert again.id == a_id
        assert again.company_id == cid
