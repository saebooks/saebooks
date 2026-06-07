"""Migration 0162 — three fixes to the 0161 JE engine-guard.

These tests run with the 0161/0162 guard LIVE. The suite opens the escape
hatch by default for fixture/teardown convenience (see
``conftest._declare_db_rebuild_for_tests``); a session here opts back IN to
enforcement via ``info["je_guard"] = True`` — done for us by
:func:`guarded_session`. Without this opt-in the hatch is open and the guard
allows everything, so these assertions would be vacuous.

FIX 1 — the deployed false-positive: a legit engine UPDATE on a DRAFT row
        whose origin is still UNKNOWN (the cashbook attachments stamp) was
        rejected by 0161. (a) + the FIX-1-still-catches cases in (e).
FIX 2 — HOLE 1: a record-type origin landing POSTED/REVERSED must carry a
        real source_type + source_id. (b) reject, (c) allow.
FIX 3 — ENABLE ALWAYS: the trigger survives session_replication_role=replica.
        (f).

Coverage map (mirrors the task spec a-h):
  (a) cashbook income+expense create (registered + non-registered) -> ALLOWED
  (b) fake record-type origin + NULL source landing POSTED        -> REJECT
  (c) each require-source origin WITH source                       -> ALLOW
  (d) MANUAL+reason and each exempt origin WITHOUT source          -> ALLOW
  (e) raw flip status->POSTED leaving origin UNKNOWN -> REJECT;
      origin-scrub on a posted row -> REJECT  (FIX 1 still catches)
  (f) ENABLE ALWAYS: tgenabled='A' and a replica-mode attack INSERT -> REJECT
  (g) the declared-rebuild hatch bypasses
  (h) all 0161 cases preserved (delegated to tests/db/test_je_engine_guard.py,
      whose record-type ALLOW cases were updated to pass source under FIX 2)
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from saebooks.db import AsyncSessionLocal
from saebooks.models.journal import JournalOrigin

# Reuse the proven helpers from the 0161 test module so the harness is
# identical (guarded_session opts the guard IN; _raw_insert_entry /
# _force_delete drive raw SQL with the guard live / via the hatch).
from tests.db.test_je_engine_guard import (
    _force_delete,
    _raw_insert_entry,
    guarded_session,
)

pytestmark = pytest.mark.postgres_only


# REQUIRE-SOURCE / EXEMPT mirror the migration's classification (kept in lock
# step with services/*.py — see 0162 docstring). The test asserts the live
# trigger agrees with this split.
_REQUIRE_SOURCE = [
    JournalOrigin.INVOICE,
    JournalOrigin.BILL,
    JournalOrigin.PAYMENT,
    JournalOrigin.EXPENSE,
    JournalOrigin.CREDIT_NOTE,
    JournalOrigin.SUPPLIER_CREDIT_NOTE,
    JournalOrigin.RECEIPT,
    JournalOrigin.TRANSFER,
    JournalOrigin.RECLASSIFICATION,
    JournalOrigin.INTERCOMPANY,
    JournalOrigin.DEPRECIATION,
    JournalOrigin.FIXED_ASSET,
    JournalOrigin.BANK_REC,
    JournalOrigin.PAYRUN,
    JournalOrigin.TRUST_DISTRIBUTION,
    JournalOrigin.REVERSAL,
]
_EXEMPT_NO_SOURCE = [
    JournalOrigin.CASHBOOK_BACKFILL,
    JournalOrigin.DEFERRED_REVENUE,
    JournalOrigin.YEAR_END_CLOSE,
    JournalOrigin.FX_REVAL,
]


async def _add_balanced_lines(s, je_id: uuid.UUID, accts) -> None:
    """Two balanced lines so the 0101 COMMIT-time balance trigger is happy."""
    for ln, (acct, debit, credit) in enumerate(
        [(accts[0], "100", "0"), (accts[1], "0", "100")], start=1
    ):
        await s.execute(
            text(
                "INSERT INTO journal_lines (id, entry_id, line_no, account_id, "
                "debit, credit) VALUES (:id, :e, :ln, :acct, :d, :c)"
            ),
            {"id": uuid.uuid4(), "e": je_id, "ln": ln,
             "acct": acct, "d": debit, "c": credit},
        )


async def _raw_insert_with_source(
    cid, tid, accts, *, status: str, origin: str,
    source_type: str | None, source_id: uuid.UUID | None,
    override_reason: str | None = None,
) -> uuid.UUID:
    """Raw-INSERT a header (+ balanced lines if POSTED-like) with explicit
    source_* — the guard is LIVE. Used to drive FIX 2 (b)/(c)/(d)."""
    je_id = uuid.uuid4()
    ref = f"G62-{je_id.hex[:8]}"
    async with guarded_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO journal_entries
                    (id, company_id, tenant_id, ref, entry_date, description,
                     status, origin, override_reason, source_type, source_id,
                     version)
                VALUES
                    (:id, :cid, :tid, :ref, '2026-05-01', 'guard62 test',
                     :status, :origin, :reason, :st, :sid, 1)
                """
            ),
            {"id": je_id, "cid": cid, "tid": tid, "ref": ref,
             "status": status, "origin": origin, "reason": override_reason,
             "st": source_type, "sid": source_id},
        )
        if status in ("POSTED", "REVERSED"):
            await _add_balanced_lines(s, je_id, accts)
        await s.commit()
    return je_id


# ---------------------------------------------------------------------------
# (a) FIX 1 regression — the cashbook create path through the REAL service.
#     record_cashbook_entry does create_draft (DRAFT/UNKNOWN) -> stamp
#     attachments -> flush (UPDATE, status still DRAFT + origin still UNKNOWN)
#     -> post(origin=CASHBOOK_BACKFILL). 0161 trips on the flush's UPDATE;
#     0162 must allow it. Cash-basis, registered + non-registered, both
#     directions. The session opts the guard IN (je_guard=True), so this is a
#     true under-guard regression — the existing cashbook suite runs hatch-open.
# ---------------------------------------------------------------------------
async def _seed_cashbook_company(*, gst_registered: bool):
    """Flip the shared AU-seed company into cashbook mode (mirrors
    tests/services/test_cashbook.py::_seed_company_into_cashbook_mode).
    Returns (tenant_id, company_id)."""
    from sqlalchemy import select

    from saebooks.models.account import Account
    from saebooks.models.company import Company
    from saebooks.services import settings as settings_svc

    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company not found — check conftest seed_coa"
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id, Account.code == "1-1110"
                )
            )
        ).scalar_one_or_none()
        assert bank is not None, "AU CoA seed missing 1-1110 Bank"
        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.gst_registered = gst_registered
        if gst_registered:
            await settings_svc.set(session, "gst_collected_account_code", "2-1310")
            await settings_svc.set(session, "gst_paid_account_code", "2-1330")
            await settings_svc.set(session, "gst_auto_post", "true")
        await session.commit()
        return co.tenant_id, co.id


@pytest.fixture
async def _restore_seed_company():
    """Reset the shared seed company back to 'full' after the test so the
    cashbook-mode mutation doesn't leak (CHECK ck_cashbook_requires_bank
    needs both columns reset atomically)."""
    yield
    from sqlalchemy import select

    from saebooks.models.company import Company

    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        if co is None:
            return
        await session.execute(
            text(
                "UPDATE companies SET bookkeeping_mode='full', "
                "cashbook_default_bank_account_id=NULL, gst_registered=false "
                "WHERE id = :cid"
            ),
            {"cid": co.id},
        )
        await session.commit()


@pytest.mark.parametrize("gst_registered", [True, False])
@pytest.mark.parametrize(
    "direction,category_code",
    [("income", "INC_SERVICES"), ("expense", "EXP_MATERIALS")],
)
async def test_cashbook_create_under_guard_is_allowed(
    _restore_seed_company, gst_registered, direction, category_code
):
    """Drive record_cashbook_entry with the 0162 guard LIVE on the session.
    Under 0161 this raised je_engine_guard at the attachments flush; 0162
    allows it (FIX 1) and CASHBOOK_BACKFILL with NULL source is EXEMPT under
    FIX 2."""
    from saebooks.services.cashbook import record_cashbook_entry

    tenant_id, company_id = await _seed_cashbook_company(gst_registered=gst_registered)
    async with guarded_session() as s:
        je = await record_cashbook_entry(
            db=s,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 5, 1),
            description=f"cashbook {direction} guard62",
            amount=Decimal("110.00"),
            direction=direction,
            category_code=category_code,
            idempotency_key=f"g62-{direction}-{gst_registered}-{uuid.uuid4().hex[:8]}",
            actor="guard62-test",
        )
    assert je.status.value == "POSTED"
    # Cashbook stamps CASHBOOK_BACKFILL with NULL source (an EXEMPT origin).
    assert je.origin == JournalOrigin.CASHBOOK_BACKFILL
    assert je.source_type is None and je.source_id is None
    await _force_delete(je.id)


async def test_draft_attachments_stamp_under_guard_is_allowed(seeded_company):
    """Lower-level pin of FIX 1 independent of the cashbook signature: an
    UPDATE that sets attachments on a DRAFT/UNKNOWN row must pass (0161
    rejected this; 0162 allows it)."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_entry(cid, tid, accts, status="DRAFT", origin="UNKNOWN")
    async with guarded_session() as s:
        await s.execute(
            text(
                "UPDATE journal_entries SET attachments = "
                "jsonb_build_object('cashbook_meta', "
                "jsonb_build_object('direction','income')) WHERE id = :id"
            ),
            {"id": je_id},
        )
        await s.commit()
    await _force_delete(je_id)


# ---------------------------------------------------------------------------
# (b) FIX 2 — fake record-type origin + NULL source landing POSTED -> REJECT
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("origin", [o.value for o in _REQUIRE_SOURCE])
async def test_record_type_origin_null_source_posted_is_rejected(
    seeded_company, origin
):
    cid, tid, accts = seeded_company
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        await _raw_insert_with_source(
            cid, tid, accts, status="POSTED", origin=origin,
            source_type=None, source_id=None,
        )


async def test_record_type_origin_partial_source_is_rejected(seeded_company):
    """source_type set but source_id NULL (and vice-versa) is still forged."""
    cid, tid, accts = seeded_company
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        await _raw_insert_with_source(
            cid, tid, accts, status="POSTED", origin="INVOICE",
            source_type="invoice", source_id=None,
        )
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        await _raw_insert_with_source(
            cid, tid, accts, status="POSTED", origin="INVOICE",
            source_type=None, source_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# (c) FIX 2 — each require-source origin WITH source -> ALLOW
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("origin", [o.value for o in _REQUIRE_SOURCE])
async def test_record_type_origin_with_source_is_allowed(seeded_company, origin):
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_with_source(
        cid, tid, accts, status="POSTED", origin=origin,
        source_type=origin.lower(), source_id=uuid.uuid4(),
    )
    await _force_delete(je_id)


# ---------------------------------------------------------------------------
# (d) MANUAL+reason and each exempt origin WITHOUT source -> ALLOW
# ---------------------------------------------------------------------------
async def test_manual_with_reason_no_source_is_allowed(seeded_company):
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_with_source(
        cid, tid, accts, status="POSTED", origin="MANUAL",
        source_type=None, source_id=None,
        override_reason="year-end reclassification per accountant",
    )
    await _force_delete(je_id)


@pytest.mark.parametrize("origin", [o.value for o in _EXEMPT_NO_SOURCE])
async def test_exempt_origin_no_source_posted_is_allowed(seeded_company, origin):
    """CASHBOOK_BACKFILL / DEFERRED_REVENUE / YEAR_END_CLOSE / FX_REVAL post
    without a source by design — must be allowed."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_with_source(
        cid, tid, accts, status="POSTED", origin=origin,
        source_type=None, source_id=None,
    )
    await _force_delete(je_id)


# ---------------------------------------------------------------------------
# (e) FIX 1 STILL catches the two raw-bypass signatures.
# ---------------------------------------------------------------------------
async def test_raw_flip_to_posted_leaving_unknown_is_rejected(seeded_company):
    """A raw UPDATE flipping status->POSTED while leaving origin=UNKNOWN is
    still refused (NEW.status=POSTED + NEW.origin=UNKNOWN)."""
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


async def test_origin_scrub_on_posted_is_rejected(seeded_company):
    """Scrubbing origin back to UNKNOWN on a posted row is still refused."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_with_source(
        cid, tid, accts, status="POSTED", origin="INVOICE",
        source_type="invoice", source_id=uuid.uuid4(),
    )
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text("UPDATE journal_entries SET origin='UNKNOWN' WHERE id=:id"),
                {"id": je_id},
            )
            await s.commit()
    await _force_delete(je_id)


async def test_source_scrub_on_posted_record_type_is_rejected(seeded_company):
    """FIX 2 UPDATE side: nulling source on a posted record-type row -> REJECT."""
    cid, tid, accts = seeded_company
    je_id = await _raw_insert_with_source(
        cid, tid, accts, status="POSTED", origin="INVOICE",
        source_type="invoice", source_id=uuid.uuid4(),
    )
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(
                text(
                    "UPDATE journal_entries SET source_type=NULL, "
                    "source_id=NULL WHERE id=:id"
                ),
                {"id": je_id},
            )
            await s.commit()
    await _force_delete(je_id)


# ---------------------------------------------------------------------------
# (f) FIX 3 — ENABLE ALWAYS: tgenabled='A' and the guard survives replica mode.
# ---------------------------------------------------------------------------
async def test_trigger_is_enable_always() -> None:
    async with AsyncSessionLocal() as s:
        tgenabled = (
            await s.execute(
                text(
                    "SELECT tgenabled FROM pg_trigger "
                    "WHERE tgname = 'trg_je_engine_guard'"
                )
            )
        ).scalar_one()
    # pg_trigger.tgenabled is the catalog "char" type; asyncpg surfaces it as
    # a one-byte bytes value (b'A'), psycopg as the str 'A'. Normalise both.
    if isinstance(tgenabled, (bytes, bytearray)):
        tgenabled = tgenabled.decode()
    assert tgenabled == "A", f"expected ENABLE ALWAYS (A), got {tgenabled!r}"


async def test_replica_mode_attack_insert_is_rejected(seeded_company):
    """Under SET session_replication_role='replica' a normal 'O' trigger would
    NOT fire — ENABLE ALWAYS keeps the guard active, so the forged INSERT is
    still rejected."""
    cid, tid, _accts = seeded_company
    je_id = uuid.uuid4()
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(text("SET LOCAL session_replication_role = 'replica'"))
            await s.execute(
                text(
                    "INSERT INTO journal_entries (id,company_id,tenant_id,ref,"
                    "entry_date,status,origin,version) VALUES "
                    "(:id,:cid,:tid,:ref,'2026-05-01','POSTED','UNKNOWN',1)"
                ),
                {"id": je_id, "cid": cid, "tid": tid, "ref": f"REP-{je_id.hex[:8]}"},
            )
            await s.commit()


async def test_replica_mode_forged_record_type_is_rejected(seeded_company):
    """Replica-mode forged record-type origin (origin=INVOICE, NULL source)
    is still caught by FIX 2 because the trigger is ENABLE ALWAYS."""
    cid, tid, _accts = seeded_company
    je_id = uuid.uuid4()
    with pytest.raises(IntegrityError, match="je_engine_guard"):
        async with guarded_session() as s:
            await s.execute(text("SET LOCAL session_replication_role = 'replica'"))
            await s.execute(
                text(
                    "INSERT INTO journal_entries (id,company_id,tenant_id,ref,"
                    "entry_date,status,origin,source_type,source_id,version) "
                    "VALUES (:id,:cid,:tid,:ref,'2026-05-01','POSTED','INVOICE',"
                    "NULL,NULL,1)"
                ),
                {"id": je_id, "cid": cid, "tid": tid, "ref": f"REP-{je_id.hex[:8]}"},
            )
            await s.commit()


# ---------------------------------------------------------------------------
# (g) the declared-rebuild hatch bypasses every check (incl. FIX 2).
# ---------------------------------------------------------------------------
async def test_hatch_allows_forged_record_type_origin(seeded_company):
    """A declared rebuild may raw-insert a POSTED record-type origin with NULL
    source (e.g. re-importing rows whose source is reattached later)."""
    cid, tid, accts = seeded_company
    je_id = uuid.uuid4()
    async with guarded_session() as s:
        await s.execute(text("SET LOCAL app.db_rebuild = 'on'"))
        await s.execute(
            text(
                "INSERT INTO journal_entries (id,company_id,tenant_id,ref,"
                "entry_date,status,origin,version) VALUES "
                "(:id,:cid,:tid,:ref,'2026-05-01','POSTED','INVOICE',1)"
            ),
            {"id": je_id, "cid": cid, "tid": tid, "ref": f"HB-{je_id.hex[:8]}"},
        )
        await _add_balanced_lines(s, je_id, accts)
        await s.commit()
    await _force_delete(je_id)
