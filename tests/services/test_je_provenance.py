"""JE-provenance keystone — origin / source_type / source_id stamping.

Covers the posting chokepoint (``journal.post`` / ``post_in_txn``):
  - the default (no machine origin declared) flags MANUAL — the visible
    exception this keystone exists to surface;
  - an explicit origin + source linkage is persisted;
  - a reversal self-declares origin=REVERSAL linked to the original entry;
  - pre-provenance behaviour: a freshly created DRAFT carries the DB default
    UNKNOWN until it is posted (forward-only, no backfill).
"""
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.journal import EntryStatus, JournalOrigin
from saebooks.services import journal as jsvc
from saebooks.services import journal_entries as jesvc

pytestmark = pytest.mark.postgres_only


async def _draft(session, company_id, tenant_id, accounts):
    return await jesvc.create(
        session,
        company_id,
        tenant_id,
        actor="test:provenance",
        entry_date=date(2026, 5, 1),
        narration="prov",
        lines=[
            {"account_id": str(accounts[0]), "debit": Decimal("10"), "credit": Decimal("0")},
            {"account_id": str(accounts[1]), "debit": Decimal("0"), "credit": Decimal("10")},
        ],
    )


async def test_fresh_draft_is_unknown(seeded_company):
    """Forward-only: a created-but-unposted entry carries the DB default."""
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        eid = e.id
    async with AsyncSessionLocal() as s2:
        again = await jsvc.get(s2, eid, tenant_id=tid)
        assert again.status == EntryStatus.DRAFT
        assert again.origin == JournalOrigin.UNKNOWN
        assert again.source_type is None
        assert again.source_id is None


async def test_bare_post_flags_manual(seeded_company):
    """A post with no declared machine origin is the MANUAL exception."""
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        eid = e.id
        await jsvc.post(session, eid, tenant_id=tid)
    async with AsyncSessionLocal() as s2:
        again = await jsvc.get(s2, eid, tenant_id=tid)
        assert again.status == EntryStatus.POSTED
        assert again.origin == JournalOrigin.MANUAL
        assert again.source_type is None
        assert again.source_id is None


async def test_explicit_origin_and_source_persisted(seeded_company):
    """An auto-posting caller's origin + source linkage round-trips."""
    import uuid

    cid, tid, accts = seeded_company
    fake_invoice_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        eid = e.id
        await jsvc.post(
            session,
            eid,
            tenant_id=tid,
            origin=JournalOrigin.INVOICE,
            source_type="invoice",
            source_id=fake_invoice_id,
        )
    async with AsyncSessionLocal() as s2:
        again = await jsvc.get(s2, eid, tenant_id=tid)
        assert again.origin == JournalOrigin.INVOICE
        assert again.source_type == "invoice"
        assert again.source_id == fake_invoice_id


async def test_post_in_txn_stamps_before_commit(seeded_company):
    """The chokepoint stamps in-txn (post_in_txn), not only in the wrapper."""
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        eid = e.id
        posted = await jsvc.post_in_txn(
            session, eid, tenant_id=tid, origin=JournalOrigin.BILL,
            source_type="bill",
        )
        assert posted.origin == JournalOrigin.BILL
        assert posted.source_type == "bill"
        await session.commit()


async def test_reversal_declares_reversal_origin(seeded_company):
    """A reversal self-declares origin=REVERSAL linked to the original."""
    cid, tid, accts = seeded_company
    async with AsyncSessionLocal() as session:
        e = await _draft(session, cid, tid, accts)
        eid = e.id
        await jsvc.post(
            session, eid, tenant_id=tid, origin=JournalOrigin.INVOICE,
            source_type="invoice", source_id=eid,
        )
    async with AsyncSessionLocal() as s2:
        rev = await jsvc.reverse(s2, eid, tenant_id=tid)
        rev_id = rev.id
    async with AsyncSessionLocal() as s3:
        again = await jsvc.get(s3, rev_id, tenant_id=tid)
        assert again.origin == JournalOrigin.REVERSAL
        assert again.source_type == "journal_entry"
        assert again.source_id == eid
