"""0161_je_engine_guard — make journal-entry engine-bypass DELIBERATE, not casual.

Why this migration exists
-------------------------
A cleanup session once did 76 RAW ``psql`` INSERTs straight into
``journal_entries`` — landing them POSTED with ``origin='UNKNOWN'``, no
``change_log`` row, no ``source_*`` provenance — bypassing the engine, the
MCP layer, RLS and the JE-provenance keystone (migration 0153). Those rows
were indistinguishable from real, engine-posted entries in every report.

Bypass must remain POSSIBLE for a *declared* db-rebuild (Richard's own
rebuilds + the rebuild passes), but it must NOT be the easy default for a
session that simply reaches for raw SQL. The discriminator is the provenance
column: the engine's posting chokepoint (``services.journal.post`` /
``post_in_txn``) ALWAYS stamps a real ``origin`` on the DRAFT→POSTED
transition; only a raw insert leaves a POSTED row at the column default
``'UNKNOWN'``.

What this trigger does (a TRIGGER, not RLS — it fires for ALL roles,
including the BYPASSRLS owner role and superuser)
-------------------------------------------------------------------------
ESCAPE HATCH (checked first, for every operation):
    IF current_setting('app.db_rebuild', true) = 'on' THEN allow everything.
A declared rebuild (Richard / rebuild passes) sets this GUC and gets the full
bypass: raw insert, delete, rewrite. ``SET LOCAL app.db_rebuild = 'on'`` is
transaction-scoped, so the declaration is per-transaction and cannot leak.

BEFORE INSERT — only a row that LANDS posted/reversed can be a bypass; the
engine never inserts a POSTED row (it inserts a DRAFT then ``post()`` UPDATEs
it to POSTED), and drafts are harmless (never in reports, freely editable):
  * status IN (POSTED, REVERSED) AND origin = 'UNKNOWN'  → REJECT
        (the 76-row raw-insert-a-posted-entry signature).
  * status IN (POSTED, REVERSED) AND origin = 'MANUAL'
        AND override_reason IS NULL                        → REJECT
        (a raw posted MANUAL entry with no written reason — the gated
        manual path requires a reason).
  * Any real record-type origin (INVOICE / BILL / PAYMENT / TRANSFER /
    RECLASSIFICATION / … / REVERSAL), or MANUAL+reason                → ALLOW.

BEFORE UPDATE:
  * NEW.origin = 'UNKNOWN'                                  → REJECT
        (no legitimate path ever sets origin back to UNKNOWN — this catches
        a raw ``UPDATE … SET status='POSTED'`` that forgets provenance, and
        any attempt to scrub the provenance column). The engine's ``post()``
        always stamps a real origin, and the future provenance backfill SETS
        origin to a real value, so neither is touched.
  * OLD.status IN (POSTED, REVERSED) AND a financial-identity column changes
    (entry_date / ref / company_id / tenant_id)            → REJECT
        (raw edit of a posted entry's accounting identity — the service layer
        forbids this; see ``services.journal.update_draft`` "Cannot edit a
        posted entry in immutable mode — reverse instead"). Legit engine
        UPDATEs on a posted row — the reversal status-flip (POSTED→REVERSED),
        a Paperless attachment, a void/archive (archived_at), the version
        bump, and the provenance backfill — change none of those four columns
        and are ALLOWED.

BEFORE DELETE:
  * OLD.status IN (POSTED, REVERSED)                        → REJECT
        (raw ``DELETE`` bypassing ``services.journal.delete``'s guard, which
        already refuses to hard-delete a posted/reversed entry — reverse it
        instead). A DRAFT delete is the legit path and is ALLOWED.

Why NOT a literal "reject every INSERT with origin='UNKNOWN'" / "reject every
UPDATE/DELETE of a posted row"
----------------------------------------------------------------------------
The engine creates drafts via ``JournalEntry(status=DRAFT)`` WITHOUT passing
``origin`` — so every legitimate draft is inserted with the model default
``origin='UNKNOWN'``. A blanket INSERT reject would reject every engine draft
and break the whole suite. Likewise the engine performs legitimate UPDATEs on
already-posted rows (reversal status-flip, Paperless attach, void/archive) and
posts reasonless MANUAL entries through ``post()`` — a blanket reject of those
would false-positive ~30 existing tests. The status-aware INSERT rule and the
financial-identity UPDATE rule above are the precise bypass signatures that
catch the raw path WITHOUT touching any sanctioned engine path.

Existing data
-------------
Untouched. The trigger fires only on NEW INSERT / UPDATE / DELETE operations;
the pre-existing ``origin='UNKNOWN'`` rows stay exactly where they are (the
provenance backfill — a future migration — will UPDATE them to a real origin,
which this trigger explicitly ALLOWS).

Reversibility
-------------
``downgrade()`` drops the trigger and its function. The table returns to "any
role can raw-insert / raw-delete / raw-edit a posted JE".

Revision ID: 0161_je_engine_guard
Revises:     0160_principal_webauthn_lookup
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0161_je_engine_guard"
down_revision: str | None = "0160_principal_webauthn_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FN_NAME = "je_engine_guard"
_TRG_NAME = "trg_je_engine_guard"

# Shared error hint appended to every rejection so the operator always knows
# the two legitimate ways forward.
# NB: embedded in single-quoted plpgsql string literals — must contain NO
# single-quote character or it breaks out of the SQL literal. (Hence
# app.db_rebuild=on without surrounding quotes in the prose.)
_HINT = (
    "use a record-type service (create_invoice / create_bill / create_payment "
    "/ transfer / ... which post the entry with a real origin), or "
    "SET app.db_rebuild=on (+ a reason) for a declared rebuild."
)


def upgrade() -> None:
    # The guard function. plpgsql so we can branch on TG_OP and RAISE with a
    # descriptive message. SECURITY INVOKER (default) is correct — the check
    # is on the row content, not a cross-table read needing elevated rights.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FN_NAME}()
            RETURNS trigger AS $$
            BEGIN
                -- ESCAPE HATCH: a declared rebuild bypasses every check.
                -- current_setting(..., true) returns NULL (not an error) when
                -- the GUC is unset, so the comparison is NULL → not 'on'.
                IF current_setting('app.db_rebuild', true) = 'on' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;

                IF TG_OP = 'INSERT' THEN
                    -- Only a row that lands posted/reversed can be a bypass.
                    -- The engine inserts DRAFTs (origin defaults to UNKNOWN)
                    -- then post() UPDATEs them to POSTED with a real origin,
                    -- so a DRAFT insert is always allowed.
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
                    -- Never let provenance be scrubbed to UNKNOWN. The engine
                    -- post() and the future backfill both set a REAL origin,
                    -- so this only fires on a raw post-without-provenance or a
                    -- deliberate origin scrub.
                    IF NEW.origin = 'UNKNOWN' THEN
                        RAISE EXCEPTION
                            'je_engine_guard: refusing UPDATE that sets '
                            'origin=UNKNOWN on entry % (provenance must not be '
                            'erased) — %', NEW.id, '{_HINT}'
                            USING ERRCODE = 'check_violation';
                    END IF;

                    -- A posted/reversed entry's accounting identity is
                    -- immutable. Legit engine UPDATEs (reversal status-flip,
                    -- Paperless attach, void/archive, version bump, provenance
                    -- backfill) never touch these four columns; a raw edit of
                    -- a posted entry does.
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

                -- Unreachable (trigger registered only for I/U/D) but keep
                -- plpgsql happy.
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )

    # Drop-if-exists keeps the migration idempotent on re-run / partial state.
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRG_NAME} ON journal_entries"))
    # BEFORE so the RAISE aborts the statement before any row is written /
    # removed. FOR EACH ROW so OLD/NEW are available.
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_TRG_NAME} "
            f"BEFORE INSERT OR UPDATE OR DELETE ON journal_entries "
            f"FOR EACH ROW EXECUTE FUNCTION {_FN_NAME}()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRG_NAME} ON journal_entries"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FN_NAME}()"))
