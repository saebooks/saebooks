"""Task 3 — journal.delete refuses posted/reversed + wrong-company hard-delete."""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.services import journal as jsvc
from saebooks.services import journal_entries as jesvc
from saebooks.services.journal import PostingError

pytestmark = pytest.mark.postgres_only


async def _posted(session, cid, tid, accts):
    e = await jesvc.create(
        session, cid, tid, actor="t", entry_date=date(2026, 5, 1), narration="t",
        lines=[
            {"account_id": str(accts[0]), "debit": Decimal("5"), "credit": Decimal("0")},
            {"account_id": str(accts[1]), "debit": Decimal("0"), "credit": Decimal("5")},
        ],
    )
    await jsvc.post(session, e.id, tenant_id=tid)
    return e


async def test_delete_posted_is_refused(seeded_company):
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as s:
        e = await _posted(s, cid, tid, accts)
        eid = e.id
        with pytest.raises(PostingError):
            await jsvc.delete(s, eid, tenant_id=tid, company_id=cid)


async def test_delete_wrong_company_is_refused(seeded_company):
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as s:
        e = await jesvc.create(
            s, cid, tid, actor="t", entry_date=date(2026, 5, 1), narration="draft",
            lines=[
                {"account_id": str(accts[0]), "debit": Decimal("5"), "credit": Decimal("0")},
                {"account_id": str(accts[1]), "debit": Decimal("0"), "credit": Decimal("5")},
            ],
        )
        eid = e.id
        with pytest.raises(PostingError):
            await jsvc.delete(s, eid, tenant_id=tid, company_id=uuid.uuid4())


async def test_delete_draft_owned_still_works(seeded_company):
    """Backwards-compatible: a DRAFT entry owned by the company still deletes."""
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as s:
        e = await jesvc.create(
            s, cid, tid, actor="t", entry_date=date(2026, 5, 1), narration="draft-ok",
            lines=[
                {"account_id": str(accts[0]), "debit": Decimal("5"), "credit": Decimal("0")},
                {"account_id": str(accts[1]), "debit": Decimal("0"), "credit": Decimal("5")},
            ],
        )
        eid = e.id
        await jsvc.delete(s, eid, tenant_id=tid, company_id=cid)
    async with AsyncSessionLocal() as s2:
        with pytest.raises(ValueError):
            await jsvc.get(s2, eid, tenant_id=tid)
