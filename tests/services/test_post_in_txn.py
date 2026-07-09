"""Task 1 — post_in_txn(commit=False) is composable; post() commits."""
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.journal import EntryStatus
from saebooks.services import journal as jsvc
from saebooks.services import journal_entries as jesvc

pytestmark = pytest.mark.postgres_only


async def _draft(session, company_id, tenant_id, accounts):
    return await jesvc.create(
        session,
        company_id,
        tenant_id,
        actor="test:post_in_txn",
        entry_date=date(2026, 5, 1),
        narration="t",
        lines=[
            {"account_id": str(accounts[0]), "debit": Decimal("10"), "credit": Decimal("0")},
            {"account_id": str(accounts[1]), "debit": Decimal("0"), "credit": Decimal("10")},
        ],
    )


async def test_post_in_txn_does_not_commit(seeded_company):
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        entry_id = e.id
        await jsvc.post_in_txn(session, entry_id, tenant_id=tid)
        # post_in_txn must NOT have committed — rolling back undoes the post.
        await session.rollback()
    async with AsyncSessionLocal() as s2:
        again = await jsvc.get(s2, entry_id, tenant_id=tid)
        assert again.status == EntryStatus.DRAFT


async def test_post_wrapper_commits(seeded_company):
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        entry_id = e.id
        await jsvc.post(session, entry_id, tenant_id=tid)
    async with AsyncSessionLocal() as s2:
        again = await jsvc.get(s2, entry_id, tenant_id=tid)
        assert again.status == EntryStatus.POSTED
