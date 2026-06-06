"""Migration 0161 — the JE engine-lane guard makes bypass DELIBERATE.

A cleanup session once did 76 RAW ``psql`` INSERTs into ``journal_entries``
that landed POSTED with ``origin='UNKNOWN'``, no ``change_log`` and no
provenance — invisible against real engine-posted rows. The 0161 trigger
fires for ALL roles (it is a trigger, not RLS) and:

  * rejects a raw INSERT that lands a POSTED/REVERSED row with
    ``origin='UNKNOWN'`` (the bypass signature), or ``origin='MANUAL'`` with
    no ``override_reason`` (the gated manual path);
  * allows every real record-type origin, and ``MANUAL`` + reason;
  * never touches a DRAFT insert (the engine creates drafts with the default
    ``origin='UNKNOWN'`` then ``post()`` stamps the real origin);
  * rejects a raw DELETE / a raw financial-identity edit (entry_date / ref /
    company_id / tenant_id) of a POSTED/REVERSED row — the raw bypass of the
    service-layer delete/immutability guards;
  * never blocks the provenance backfill (an UPDATE that SETS origin to a real
    value on an existing row) nor the legit posted-row UPDATEs (reversal
    status-flip, attachments, archive, version bump);
  * lets a DECLARED rebuild through unconditionally when
    ``app.db_rebuild='on'`` is set on the transaction.

Every test drops to raw SQL on purpose: the point of a trigger is that going
*around* the service layer is what must fail. Asserting via the service would
test the helper, not the trigger.

These tests run with the 0161 guard LIVE. The suite opens the escape hatch by
default for fixture/teardown convenience (see ``conftest._declare_db_rebuild_
for_tests``); a session here opts back IN to enforcement via
``info["je_guard"] = True`` — done for us by :func:`guarded_session`.

POSTED/REVERSED rows created here carry two balanced journal_lines so the
*other* DB invariant — the 0101 deferred balance/line-count constraint
trigger, which fires at COMMIT — is satisfied and does not mask the 0161
BEFORE trigger we are actually exercising.
"""
from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal

pytestmark = pytest.mark.postgres_only

_POSTED_LIKE = ("POSTED", "REVERSED")


@contextlib.asynccontextmanager
async def guarded_session() -> AsyncIterator[AsyncSession]:
    """A session with the 0161 guard LIVE (opts out of the suite-wide hatch)."""
    async with AsyncSessionLocal() as s:
        s.info["je_guard"] = True
        yield s


async def _add_balanced_lines(s: AsyncSession, je_id: uuid.UUID, accts) -> None:
    """Two balanced lines so the 0101 balance/line-count trigger is satisfied."""
    for ln, (acct, debit, credit) in enumerate(
        [(accts[0], "100", "0"), (accts[1], "0", "100")], start=1
    ):
        await s.execute(
            text(
                "INSERT INTO journal_lines (id, entry_id, line_no, account_id, "
                "debit, credit) VALUES (:id, :e, :ln, :acct, :d, :c)"
            ),
            {
                "id": uuid.uuid4(), "e": je_id, "ln": ln,
                "acct": acct, "d": debit, "c": credit,
            },
        )


async def _raw_insert_entry(
    cid: uuid.UUID,
    tid: uuid.UUID,
    accts,
    *,
    status: str,
    origin: str,
    override_reason: str | None = None,
) -> uuid.UUID:
    """Raw-INSERT a journal_entries header (no service layer), guard LIVE.

    POSTED/REVERSED rows get two balanced lines (0101 trigger) UNLESS the
    insert is expected to be rejected by the 0161 BEFORE trigger first.
    """
    je_id = uuid.uuid4()
    ref = f"GRD-{je_id.hex[:8]}"
    async with guarded_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO journal_entries
                    (id, company_id, tenant_id, ref, entry_date,
                     description, status, origin, override_reason, version)
                VALUES
                    (:id, :cid, :tid, :ref, '2026-05-01',
                     'guard test', :status, :origin, :reason, 1)
                """
            ),
            {
                "id": je_id, "cid": cid, "tid": tid, "ref": ref,
                "status": status, "origin": origin, "reason": override_reason,
            },
        )
        if status in _POSTED_LIKE:
            await _add_balanced_lines(s, je_id, accts)
        await s.commit()
    return je_id


async def _force_delete(je_id: uuid.UUID) -> None:
    """Cleanup helper — remove a JE regardless of status via the declared
    rebuild hatch (the default for an un-marked test session)."""
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET LOCAL app.db_rebuild = 'on'"))
        await s.execute(
            text("DELETE FROM journal_lines WHERE entry_id = :id"), {"id": je_id}
        )
        await s.execute(
            text("DELETE FROM journal_entries WHERE id = :id"), {"id": je_id}
        )
        await s.commit()


# --------------------------------------------------------------------------
# INSERT
# --------------------------------------------------------------------------
async def test_raw_insert_posted_unknown_is_rejected(seeded_company):
    """The 76-row bug: a raw POSTED insert with origin=UNKNOWN is refused.

    The 0161 BEFORE trigger fires at INSERT time, before any line is added and
    before the COMMIT-time 0101 balance trigger — so the header alone suffices.
    """
    cid, tid, accts = seeded_company
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="UNKNOWN")


async def test_raw_insert_reversed_unknown_is_rejected(seeded_company):
    cid, tid, accts = seeded_company
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        await _raw_insert_entry(cid, tid, accts, status="REVERSED", origin="UNKNOWN")


@pytest.mark.parametrize(
    "origin",
    [
        "INVOICE", "BILL", "PAYMENT", "EXPENSE", "CREDIT_NOTE",
        "SUPPLIER_CREDIT_NOTE", "RECEIPT", "TRANSFER", "RECLASSIFICATION",
        "INTERCOMPANY", "DEPRECIATION", "FIXED_ASSET", "FX_REVAL",
        "DEFERRED_REVENUE", "BANK_REC", "YEAR_END_CLOSE", "TRUST_DISTRIBUTION",
        "CASHBOOK_BACKFILL", "REVERSAL", "PAYRUN",
    ],
)
async def test_record_type_origin_post_is_allowed(seeded_company, origin):
    """Every real record-type origin posts cleanly (no allow-list drift)."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin=origin)
    await _force_delete(je_id)


async def test_manual_post_with_reason_is_allowed(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(
        cid, tid, accts, status="POSTED", origin="MANUAL",
        override_reason="year-end reclassification per accountant",
    )
    await _force_delete(je_id)


async def test_manual_post_without_reason_is_rejected(seeded_company):
    cid, tid, accts = seeded_company
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="MANUAL")


async def test_draft_insert_unknown_is_allowed(seeded_company):
    """The engine creates drafts with the default origin=UNKNOWN — must pass."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="DRAFT", origin="UNKNOWN")
    await _force_delete(je_id)


# --------------------------------------------------------------------------
# UPDATE
# --------------------------------------------------------------------------
async def test_update_draft_to_posted_with_real_origin_is_allowed(seeded_company):
    """The engine post() DRAFT->POSTED transition stamps a real origin."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="DRAFT", origin="UNKNOWN")
    async with guarded_session() as s:
        await _add_balanced_lines(s, je_id, accts)  # 0101 needs lines once POSTED
        await s.execute(
            text("UPDATE journal_entries SET status='POSTED', origin='INVOICE' WHERE id=:id"),
            {"id": je_id},
        )
        await s.commit()
    await _force_delete(je_id)


async def test_update_draft_to_posted_manual_no_reason_is_allowed(seeded_company):
    """A sanctioned reason-less MANUAL post() (DRAFT->POSTED) is allowed — the
    ~28 existing tests post manual entries with no reason via the engine."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="DRAFT", origin="UNKNOWN")
    async with guarded_session() as s:
        await _add_balanced_lines(s, je_id, accts)
        await s.execute(
            text("UPDATE journal_entries SET status='POSTED', origin='MANUAL' WHERE id=:id"),
            {"id": je_id},
        )
        await s.commit()
    await _force_delete(je_id)


async def test_update_draft_to_posted_leaving_unknown_is_rejected(seeded_company):
    """A raw flip to POSTED that forgets to stamp provenance is refused.

    The 0161 BEFORE trigger rejects the UPDATE itself, before COMMIT — so no
    lines are needed for this case.
    """
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="DRAFT", origin="UNKNOWN")
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text("UPDATE journal_entries SET status='POSTED' WHERE id=:id"),
                {"id": je_id},
            )
            await s.commit()
    await _force_delete(je_id)


async def test_provenance_backfill_is_allowed(seeded_company):
    """An UPDATE that SETS origin to a real value on a posted row (the future
    provenance backfill) must be allowed."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    async with guarded_session() as s:
        await s.execute(
            text("UPDATE journal_entries SET origin='BILL' WHERE id=:id"),
            {"id": je_id},
        )
        await s.commit()
    await _force_delete(je_id)


async def test_update_posted_origin_to_unknown_is_rejected(seeded_company):
    """Scrubbing provenance back to UNKNOWN on a posted row is refused."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text("UPDATE journal_entries SET origin='UNKNOWN' WHERE id=:id"),
                {"id": je_id},
            )
            await s.commit()
    await _force_delete(je_id)


async def test_update_posted_status_flip_to_reversed_is_allowed(seeded_company):
    """The reversal flow flips a POSTED original to REVERSED — must be allowed."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    async with guarded_session() as s:
        await s.execute(
            text("UPDATE journal_entries SET status='REVERSED' WHERE id=:id"),
            {"id": je_id},
        )
        await s.commit()
    await _force_delete(je_id)


@pytest.mark.parametrize(
    "col,val",
    [
        ("entry_date", "'2030-01-01'"),
        ("ref", "'HACKED-REF'"),
    ],
)
async def test_raw_edit_of_posted_identity_is_rejected(seeded_company, col, val):
    """Editing a posted entry's accounting identity via raw SQL is refused."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text(f"UPDATE journal_entries SET {col}={val} WHERE id=:id"),
                {"id": je_id},
            )
            await s.commit()
    await _force_delete(je_id)


# --------------------------------------------------------------------------
# DELETE
# --------------------------------------------------------------------------
async def test_raw_delete_of_posted_is_rejected(seeded_company):
    """A raw DELETE of a posted entry bypasses the service guard — refused."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text("DELETE FROM journal_entries WHERE id=:id"), {"id": je_id}
            )
            await s.commit()
    await _force_delete(je_id)


async def test_raw_delete_of_reversed_is_rejected(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="REVERSED", origin="REVERSAL")
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text("DELETE FROM journal_entries WHERE id=:id"), {"id": je_id}
            )
            await s.commit()
    await _force_delete(je_id)


async def test_raw_delete_of_draft_is_allowed(seeded_company):
    """A DRAFT delete is the legit path (services.journal.delete allows it)."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="DRAFT", origin="UNKNOWN")
    async with guarded_session() as s:
        await s.execute(
            text("DELETE FROM journal_entries WHERE id=:id"), {"id": je_id}
        )
        await s.commit()


# --------------------------------------------------------------------------
# ESCAPE HATCH — a declared rebuild bypasses every check
# --------------------------------------------------------------------------
async def _rebuild_insert_posted(cid, tid, accts, origin) -> uuid.UUID:
    """Insert a POSTED row (with balanced lines) under a declared rebuild."""
    je_id = uuid.uuid4()
    async with guarded_session() as s:
        await s.execute(text("SET LOCAL app.db_rebuild = 'on'"))
        await s.execute(
            text(
                "INSERT INTO journal_entries (id,company_id,tenant_id,ref,"
                "entry_date,status,origin,version) VALUES "
                "(:id,:cid,:tid,:ref,'2026-05-01','POSTED',:o,1)"
            ),
            {"id": je_id, "cid": cid, "tid": tid,
             "ref": f"RB-{je_id.hex[:8]}", "o": origin},
        )
        await _add_balanced_lines(s, je_id, accts)
        await s.commit()
    return je_id


async def test_rebuild_allows_raw_posted_unknown_insert(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _rebuild_insert_posted(cid, tid, accts, "UNKNOWN")
    await _force_delete(je_id)


async def test_rebuild_allows_raw_manual_no_reason_insert(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _rebuild_insert_posted(cid, tid, accts, "MANUAL")
    await _force_delete(je_id)


async def test_rebuild_allows_raw_delete_of_posted(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    async with guarded_session() as s:
        await s.execute(text("SET LOCAL app.db_rebuild = 'on'"))
        await s.execute(
            text("DELETE FROM journal_lines WHERE entry_id=:id"), {"id": je_id}
        )
        await s.execute(
            text("DELETE FROM journal_entries WHERE id=:id"), {"id": je_id}
        )
        await s.commit()


async def test_rebuild_allows_raw_edit_of_posted_identity(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="POSTED", origin="INVOICE")
    async with guarded_session() as s:
        await s.execute(text("SET LOCAL app.db_rebuild = 'on'"))
        await s.execute(
            text("UPDATE journal_entries SET entry_date='2030-01-01' WHERE id=:id"),
            {"id": je_id},
        )
        await s.commit()
    await _force_delete(je_id)
