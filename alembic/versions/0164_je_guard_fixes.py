"""0164_je_guard_fixes — three fixes to the 0161 JE engine-guard.

Why this migration exists
-------------------------
0161 (``trg_je_engine_guard``) shipped live to all five tenants
(was alembic head ``0161_je_engine_guard``; this fix rebased to follow ``0163_contractor_contact_type`` as ``0164``). Adversarial review + the live test
run found three problems. This migration replaces the guard FUNCTION body and
hardens the trigger registration; it does NOT touch existing data.

FIX 1 — the deployed false-positive (CRITICAL)
----------------------------------------------
0161's UPDATE branch opened with an unconditional::

    IF NEW.origin = 'UNKNOWN' THEN RAISE ...

That has NO status guard, so it rejects a *legitimate engine UPDATE on a
DRAFT row whose origin is still UNKNOWN*. The cashbook create path
(``services/cashbook.py`` ~480-506) does exactly that:

    draft = create_draft(...)            # status=DRAFT, origin=UNKNOWN (default)
    draft.attachments = {..cashbook_meta..}
    await db.flush()                     # emits UPDATE journal_entries SET attachments=..
                                         #   WHERE id=..  (status still DRAFT, origin still UNKNOWN)
    ... then post() ...

The flush emits an UPDATE with NEW.origin=UNKNOWN while the row is still a
DRAFT, and 0161 trips. Verified: 11 cashbook tests fail with the guard live.

FIX: gate the UNKNOWN-scrub rule on the posted/reversed status::

    IF NEW.status IN ('POSTED','REVERSED') AND NEW.origin = 'UNKNOWN' THEN RAISE

This STILL catches both raw-bypass signatures the rule existed for:
  * a raw ``UPDATE ... SET status='POSTED'`` that forgets provenance
    (NEW.status=POSTED + NEW.origin=UNKNOWN), and
  * an origin-scrub on an already-posted row
    (NEW.status=POSTED/REVERSED + NEW.origin=UNKNOWN),
while ALLOWING the cashbook DRAFT attachments stamp (NEW.status=DRAFT).

FIX 2 — HOLE 1: require a source for record-type origins
--------------------------------------------------------
Under 0161 a superuser could raw-INSERT a POSTED row with a *fake*
record-type origin and no provenance pointer::

    INSERT ... status='POSTED', origin='INVOICE', source_type=NULL, source_id=NULL

and it landed — the origin column lied and nothing checked the source_*
columns that 0153 added precisely to anchor the origin to a real record.

FIX: for an INSERT, or an UPDATE that RESULTS IN a row with
``status IN ('POSTED','REVERSED')`` AND ``origin`` in the REQUIRE-SOURCE set,
REQUIRE ``source_type IS NOT NULL AND source_id IS NOT NULL`` else REJECT.

Classification (verified against the JournalOrigin enum in
``saebooks/models/journal.py`` AND every posting call site in
``saebooks/services/*.py`` AND the live data in all five tenant DBs):

  REQUIRE-SOURCE — the engine ALWAYS posts these with a real source record:
    INVOICE              services/invoices.py:769        source_type='invoice'
    BILL                 services/bills.py:521           source_type='bill'
    PAYMENT              services/payments.py:711        source_type='payment'
    EXPENSE              services/expenses.py:498        source_type='expense'
    CREDIT_NOTE          services/credit_notes.py:394    source_type='credit_note'
    SUPPLIER_CREDIT_NOTE services/supplier_credit_notes.py:604 source_type='supplier_credit_note'
    RECEIPT              services/receipts.py:593        source_type='receipt'
    TRANSFER             services/transfers.py:222       source_type='transfer'
    RECLASSIFICATION     services/reclassifications.py:322 source_type='reclassification'
    INTERCOMPANY         services/intercompany.py:331/340 source_type='ic_txn'
    DEPRECIATION         services/assets.py:527          source_type='fixed_asset'
    FIXED_ASSET          services/assets.py:673/782, fixed_assets.py:549 source_type='fixed_asset'
    BANK_REC             services/reconciliation.py:490, bank_rules.py:346 source_type='bank_statement_line'
    PAYRUN               services/pay_runs.py:490, pay_runs_v2.py:627 source_type='pay_run'
    TRUST_DISTRIBUTION   services/distributions.py:215   source_type='trust_distribution'
    REVERSAL             services/journal.py:810         source_type='journal_entry'

  EXEMPT — legitimately post WITHOUT a source record (verified in code AND
  confirmed src_null only ever appears for UNKNOWN in the live data check):
    UNKNOWN              model column default; pre-provenance rows + every DRAFT
    MANUAL              reason-gated already (the 0161 reason rule); no source by design
    CASHBOOK_BACKFILL  services/cashbook.py:544 — "cashbook entries ARE JEs, source stays null"
    DEFERRED_REVENUE   services/deferred_revenue.py:256 — "no single originating record"
    YEAR_END_CLOSE     services/period_close.py:238 — "rolls up many P&L accounts"
    FX_REVAL           in the enum, no posting call site yet — a system/adjustment
                        origin that, like the other roll-up origins, would post
                        without a single source record; kept EXEMPT so a future
                        fx-reval path is not pre-broken.

Empirical false-positive check (read-only, all five tenants, 2026-06-07):
EVERY existing POSTED/REVERSED row either carries source (src_null=0) or is
EXEMPT. The ONLY origin with src_null>0 is UNKNOWN (1162 in sauer_books,
1168 in sandbox; all exempt). app_preview / richard / gecairns have zero
POSTED/REVERSED rows. So FIX 2 false-positives nothing live. Full breakdown
recorded in the PR body.

The MANUAL default at the posting chokepoint (``post_in_txn`` defaults
``origin=MANUAL`` + ``source_*=None``) is covered: MANUAL is EXEMPT from the
source requirement and gated instead by the existing override_reason rule.

FIX 3 — HOLE 3 (partial): make the trigger survive replica mode
---------------------------------------------------------------
0161's trigger is ``tgenabled='O'`` (origin/default), so a writer can do
``SET session_replication_role='replica'`` (or ``ALTER TABLE ... DISABLE
TRIGGER``) and the guard stops firing. FIX: ``ALTER TABLE journal_entries
ENABLE ALWAYS TRIGGER trg_je_engine_guard`` → ``tgenabled='A'``, which fires
even under replica mode. ``downgrade()`` restores it to the default ENABLE
state (``tgenabled='O'``) and restores the 0161 function body verbatim.

HOLE 2 (documented, NO code fix here)
-------------------------------------
``app.db_rebuild`` is a *convention* (a user-settable GUC), not a privilege
boundary: any writer can ``SET app.db_rebuild=on`` and walk through the hatch.
That is by design — the hatch is a "declare your intent" speed-bump, not an
authz gate. Likewise ``TRUNCATE journal_entries`` (which does NOT fire row
triggers) and ``ALTER TABLE ... DISABLE TRIGGER`` remain available to a
superuser/table-owner. ENABLE ALWAYS (FIX 3) closes the *replica-role* defeat
specifically; full defeat-proofing against a superuser is out of scope for a
trigger and would need role separation / an event trigger / `pg_audit`.

Reversibility
-------------
``downgrade()`` is a faithful restore of the 0161 trigger registration
(default ENABLE) and the 0161 function body. No data is read or written.

Revision ID: 0164_je_guard_fixes
Revises:     0163_contractor_contact_type
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0164_je_guard_fixes"
down_revision: str | None = "0163_contractor_contact_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FN_NAME = "je_engine_guard"
_TRG_NAME = "trg_je_engine_guard"

# Origins the engine ALWAYS posts with a real source record (origin + source_*
# both anchored). A POSTED/REVERSED row carrying one of these with a NULL
# source is a forged-provenance bypass — REJECT. Kept in sync with the
# JournalOrigin enum + the services/*.py posting call sites (see docstring).
# Rendered into a plpgsql IN (...) list below — single-quoted literals, must
# contain no embedded quote.
_REQUIRE_SOURCE = (
    "INVOICE",
    "BILL",
    "PAYMENT",
    "EXPENSE",
    "CREDIT_NOTE",
    "SUPPLIER_CREDIT_NOTE",
    "RECEIPT",
    "TRANSFER",
    "RECLASSIFICATION",
    "INTERCOMPANY",
    "DEPRECIATION",
    "FIXED_ASSET",
    "BANK_REC",
    "PAYRUN",
    "TRUST_DISTRIBUTION",
    "REVERSAL",
)
# EXEMPT (post without a source by design): UNKNOWN, MANUAL, CASHBOOK_BACKFILL,
# DEFERRED_REVENUE, YEAR_END_CLOSE, FX_REVAL. NOT listed here on purpose —
# anything NOT in _REQUIRE_SOURCE is exempt from the source requirement, so a
# future exempt origin needs no migration to stay exempt.
_REQUIRE_SOURCE_SQL = ", ".join(f"'{o}'" for o in _REQUIRE_SOURCE)

_HINT = (
    "use a record-type service (create_invoice / create_bill / create_payment "
    "/ transfer / ... which post the entry with a real origin), or "
    "SET app.db_rebuild=on (+ a reason) for a declared rebuild."
)


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FN_NAME}()
            RETURNS trigger AS $$
            BEGIN
                -- ESCAPE HATCH: a declared rebuild bypasses every check.
                IF current_setting('app.db_rebuild', true) = 'on' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;

                IF TG_OP = 'INSERT' THEN
                    -- Only a row that lands posted/reversed can be a bypass.
                    IF NEW.status IN ('POSTED', 'REVERSED') THEN
                        IF NEW.origin = 'UNKNOWN' THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw INSERT of a % '
                                'journal entry with origin=UNKNOWN (no engine '
                                'provenance) — %', NEW.status, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                        IF NEW.origin = 'MANUAL' AND NEW.override_reason IS NULL THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw INSERT of a % '
                                'MANUAL journal entry with no override_reason — '
                                'a manual JE must carry a written reason; %',
                                NEW.status, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                        -- FIX 2 (HOLE 1): a record-type origin MUST carry a
                        -- real source pointer. A POSTED row claiming
                        -- origin=INVOICE with NULL source_* is forged
                        -- provenance — the source_* columns (0153) exist to
                        -- anchor the origin to a real record.
                        IF NEW.origin IN ({_REQUIRE_SOURCE_SQL})
                           AND (NEW.source_type IS NULL OR NEW.source_id IS NULL) THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw INSERT of a % '
                                'journal entry with origin=% but NULL source '
                                '(a record-type origin must carry source_type + '
                                'source_id) — %',
                                NEW.status, NEW.origin, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                    END IF;
                    RETURN NEW;

                ELSIF TG_OP = 'UPDATE' THEN
                    -- FIX 1: gate the provenance-scrub rule on POSTED/REVERSED.
                    -- 0161 rejected ANY update leaving origin=UNKNOWN, which
                    -- false-positived the cashbook DRAFT attachments stamp
                    -- (create_draft -> set attachments -> flush, still DRAFT +
                    -- UNKNOWN, before post()). Gating on status keeps both
                    -- bypass signatures (a raw flip to POSTED leaving UNKNOWN,
                    -- and an origin-scrub on an already-posted row) while
                    -- allowing the legit DRAFT update.
                    IF NEW.status IN ('POSTED', 'REVERSED')
                       AND NEW.origin = 'UNKNOWN' THEN
                        RAISE EXCEPTION
                            'je_engine_guard: refusing UPDATE that leaves a % '
                            'entry % with origin=UNKNOWN (provenance must not '
                            'be erased / a post must stamp a real origin) — %',
                            NEW.status, NEW.id, '{_HINT}'
                            USING ERRCODE = 'check_violation';
                    END IF;

                    -- FIX 2 (HOLE 1), UPDATE side: an update that RESULTS IN a
                    -- posted/reversed row with a record-type origin must also
                    -- carry source. Catches a raw flip DRAFT->POSTED that sets
                    -- origin=INVOICE but no source, and a source-scrub on a
                    -- posted record-type row.
                    IF NEW.status IN ('POSTED', 'REVERSED')
                       AND NEW.origin IN ({_REQUIRE_SOURCE_SQL})
                       AND (NEW.source_type IS NULL OR NEW.source_id IS NULL) THEN
                        RAISE EXCEPTION
                            'je_engine_guard: refusing UPDATE leaving % entry % '
                            'with origin=% but NULL source (a record-type origin '
                            'must carry source_type + source_id) — %',
                            NEW.status, NEW.id, NEW.origin, '{_HINT}'
                            USING ERRCODE = 'check_violation';
                    END IF;

                    -- A posted/reversed entry's accounting identity is
                    -- immutable.
                    IF OLD.status IN ('POSTED', 'REVERSED') THEN
                        IF NEW.entry_date IS DISTINCT FROM OLD.entry_date
                           OR NEW.ref        IS DISTINCT FROM OLD.ref
                           OR NEW.company_id IS DISTINCT FROM OLD.company_id
                           OR NEW.tenant_id  IS DISTINCT FROM OLD.tenant_id THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw edit of % entry '
                                '% (entry_date/ref/company_id/tenant_id are '
                                'immutable once posted — reverse it instead) — '
                                '%', OLD.status, OLD.id, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                    END IF;
                    RETURN NEW;

                ELSIF TG_OP = 'DELETE' THEN
                    IF OLD.status IN ('POSTED', 'REVERSED') THEN
                        RAISE EXCEPTION
                            'je_engine_guard: refusing raw DELETE of % entry % '
                            '(posted/reversed entries must be reversed, never '
                            'hard-deleted) — %', OLD.status, OLD.id, '{_HINT}'
                            USING ERRCODE = 'check_violation';
                    END IF;
                    RETURN OLD;
                END IF;

                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )

    # FIX 3: ENABLE ALWAYS so the guard fires even under
    # session_replication_role='replica'. The trigger already exists (created
    # by 0161); we only flip tgenabled 'O' -> 'A'. (If for some reason the
    # trigger is missing — e.g. 0161 downgraded then 0162 applied — recreate
    # it first so the ALTER cannot fail.)
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRG_NAME} ON journal_entries"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_TRG_NAME} "
            f"BEFORE INSERT OR UPDATE OR DELETE ON journal_entries "
            f"FOR EACH ROW EXECUTE FUNCTION {_FN_NAME}()"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE journal_entries ENABLE ALWAYS TRIGGER {_TRG_NAME}"
        )
    )


def downgrade() -> None:
    # Restore the 0161 trigger registration (default ENABLE, tgenabled='O')
    # and the 0161 function body VERBATIM.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FN_NAME}()
            RETURNS trigger AS $$
            BEGIN
                IF current_setting('app.db_rebuild', true) = 'on' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;

                IF TG_OP = 'INSERT' THEN
                    IF NEW.status IN ('POSTED', 'REVERSED') THEN
                        IF NEW.origin = 'UNKNOWN' THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw INSERT of a % '
                                'journal entry with origin=UNKNOWN (no engine '
                                'provenance) — %', NEW.status, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                        IF NEW.origin = 'MANUAL' AND NEW.override_reason IS NULL THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw INSERT of a % '
                                'MANUAL journal entry with no override_reason — '
                                'a manual JE must carry a written reason; %',
                                NEW.status, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                    END IF;
                    RETURN NEW;

                ELSIF TG_OP = 'UPDATE' THEN
                    IF NEW.origin = 'UNKNOWN' THEN
                        RAISE EXCEPTION
                            'je_engine_guard: refusing UPDATE that sets '
                            'origin=UNKNOWN on entry % (provenance must not be '
                            'erased) — %', NEW.id, '{_HINT}'
                            USING ERRCODE = 'check_violation';
                    END IF;

                    IF OLD.status IN ('POSTED', 'REVERSED') THEN
                        IF NEW.entry_date IS DISTINCT FROM OLD.entry_date
                           OR NEW.ref        IS DISTINCT FROM OLD.ref
                           OR NEW.company_id IS DISTINCT FROM OLD.company_id
                           OR NEW.tenant_id  IS DISTINCT FROM OLD.tenant_id THEN
                            RAISE EXCEPTION
                                'je_engine_guard: refusing raw edit of % entry '
                                '% (entry_date/ref/company_id/tenant_id are '
                                'immutable once posted — reverse it instead) — '
                                '%', OLD.status, OLD.id, '{_HINT}'
                                USING ERRCODE = 'check_violation';
                        END IF;
                    END IF;
                    RETURN NEW;

                ELSIF TG_OP = 'DELETE' THEN
                    IF OLD.status IN ('POSTED', 'REVERSED') THEN
                        RAISE EXCEPTION
                            'je_engine_guard: refusing raw DELETE of % entry % '
                            '(posted/reversed entries must be reversed, never '
                            'hard-deleted) — %', OLD.status, OLD.id, '{_HINT}'
                            USING ERRCODE = 'check_violation';
                    END IF;
                    RETURN OLD;
                END IF;

                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRG_NAME} ON journal_entries"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_TRG_NAME} "
            f"BEFORE INSERT OR UPDATE OR DELETE ON journal_entries "
            f"FOR EACH ROW EXECUTE FUNCTION {_FN_NAME}()"
        )
    )
    # Default ENABLE (tgenabled='O') — undo FIX 3's ENABLE ALWAYS.
    op.execute(
        sa.text(
            f"ALTER TABLE journal_entries ENABLE TRIGGER {_TRG_NAME}"
        )
    )
