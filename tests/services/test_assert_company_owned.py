"""Task 2 — assert_company_owned + CrossCompanyError (app-layer write guard)."""
import uuid

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.services.tenant import CrossCompanyError, assert_company_owned

pytestmark = pytest.mark.postgres_only


async def test_owned_passes(seeded_company):
    cid, _tid, accts = seeded_company
    async with AsyncSessionLocal() as s:
        # All accounts belong to cid — no raise.
        await assert_company_owned(s, Account, accts, cid)


async def test_foreign_raises(seeded_company):
    _cid, _tid, accts = seeded_company
    other = uuid.uuid4()
    async with AsyncSessionLocal() as s:
        with pytest.raises(CrossCompanyError):
            await assert_company_owned(s, Account, accts, other, label="Account")


async def test_none_ids_are_ignored(seeded_company):
    cid, _tid, _accts = seeded_company
    async with AsyncSessionLocal() as s:
        # None entries (e.g. optional FK) are skipped; empty set is a no-op.
        await assert_company_owned(s, Account, [None, None], cid)
