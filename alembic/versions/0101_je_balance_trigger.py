"""M1 — DB-level guard: POSTED journal entries must balance and have >=2 lines.

Audit finding (CRITICAL #1): the application enforces these invariants
in saebooks.services.journal.post_entry, but a service that bypasses
that helper — bulk-import scripts, ad-hoc SQL admin, a future router
that forgets to call the helper — can post an unbalanced or
single-line journal entry. Once that's in the ledger every downstream
report is silently wrong.

This migration adds two CONSTRAINT TRIGGERs (DEFERRABLE INITIALLY
DEFERRED) so the check fires once per affected JE at COMMIT, not on
each intermediate INSERT/UPDATE during a flush. That lets the
ordinary "INSERT entry DRAFT → INSERT lines → UPDATE status POSTED"
sequence inside a single transaction succeed: the trigger sees only
the final committed state.

Behaviour
---------
- DRAFT (or any non-POSTED) entries: no enforcement. Drafts can be
  unbalanced, single-line, lineless. They cannot affect reports
  because every report query joins on status = 'POSTED'.
- POSTED entries at COMMIT must satisfy:
    1. SUM(debit) = SUM(credit) across all lines (in cents — Numeric(14,2)).
    2. COUNT(lines) >= 2.

Existing data
-------------
CONSTRAINT TRIGGERs only fire on subsequent operations; pre-existing
violations are not retroactively rejected. The migration logs a
count of any existing POSTED entries that fail the new rule so an
operator can decide whether to repair.

Reversibility
-------------
``downgrade()`` drops both triggers and the function. The DB returns
to "any service can post any JE without DB-level validation".

Revision ID: 0101_je_balance_trigger
Revises: 0100_multi_jurisdiction_company
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0101_je_balance_trigger"
down_revision: str | None = "0100_multi_jurisdiction_company"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FN_NAME = "check_je_posted_balanced_and_has_lines"


def upgrade() -> None:
    bind = op.get_bind()

    # 1. The validation function. Looks up the JE row, returns early
    #    for non-POSTED, otherwise asserts balance + line count.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FN_NAME}(p_entry_id uuid)
            RETURNS void AS $$
            DECLARE
                v_status text;
                v_debit numeric;
                v_credit numeric;
                v_line_count integer;
            BEGIN
                SELECT status::text INTO v_status
                FROM journal_entries
                WHERE id = p_entry_id;

                -- JE may have been DELETEd in the same tx (cascade
                -- from companies, etc.) — nothing to validate.
                IF NOT FOUND THEN
                    RETURN;
                END IF;

                IF v_status IS DISTINCT FROM 'POSTED' THEN
                    RETURN;
                END IF;

                SELECT
                    COALESCE(SUM(debit), 0),
                    COALESCE(SUM(credit), 0),
                    COUNT(*)
                INTO v_debit, v_credit, v_line_count
                FROM journal_lines
                WHERE entry_id = p_entry_id;

                IF v_line_count < 2 THEN
                    RAISE EXCEPTION
                        'POSTED journal entry % has % line(s); minimum 2 required',
                        p_entry_id, v_line_count
                        USING ERRCODE = 'check_violation';
                END IF;

                IF v_debit IS DISTINCT FROM v_credit THEN
                    RAISE EXCEPTION
                        'POSTED journal entry % unbalanced: debits=%, credits=%, delta=%',
                        p_entry_id, v_debit, v_credit, (v_debit - v_credit)
                        USING ERRCODE = 'check_violation';
                END IF;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )

    # 2. Wrapper trigger functions. CONSTRAINT TRIGGER passes NEW/OLD
    #    via the trigger context, but plpgsql can't take both
    #    nullable values cleanly across INSERT/UPDATE/DELETE — easier
    #    to write two thin wrappers that pull the relevant entry id.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION trg_je_balance_after_je_change()
            RETURNS trigger AS $$
            BEGIN
                -- For DELETE on journal_entries the row is gone; the
                -- inner function handles NOT FOUND by returning early.
                IF TG_OP = 'DELETE' THEN
                    PERFORM check_je_posted_balanced_and_has_lines(OLD.id);
                ELSE
                    PERFORM check_je_posted_balanced_and_has_lines(NEW.id);
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION trg_je_balance_after_jl_change()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    PERFORM check_je_posted_balanced_and_has_lines(OLD.entry_id);
                ELSE
                    PERFORM check_je_posted_balanced_and_has_lines(NEW.entry_id);
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )

    # 3. The constraint triggers. DEFERRABLE INITIALLY DEFERRED so
    #    they fire once at COMMIT, after all flushes in the tx have
    #    settled. FOR EACH ROW because that's the only mode constraint
    #    triggers support.
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER trg_je_balance_je
            AFTER INSERT OR UPDATE ON journal_entries
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION trg_je_balance_after_je_change();
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER trg_je_balance_jl
            AFTER INSERT OR UPDATE OR DELETE ON journal_lines
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION trg_je_balance_after_jl_change();
            """
        )
    )

    # 4. Log pre-existing violations so an operator can repair them.
    #    Doesn't fail the migration — the new rule binds future writes
    #    only, exactly like 0055/0056.
    rows = bind.execute(
        sa.text(
            """
            WITH je_stats AS (
                SELECT
                    je.id,
                    je.ref,
                    je.company_id,
                    COALESCE(SUM(jl.debit), 0)  AS sum_debit,
                    COALESCE(SUM(jl.credit), 0) AS sum_credit,
                    COUNT(jl.id)                AS line_count
                FROM journal_entries je
                LEFT JOIN journal_lines jl ON jl.entry_id = je.id
                WHERE je.status::text = 'POSTED'
                GROUP BY je.id, je.ref, je.company_id
            )
            SELECT id, ref, company_id, sum_debit, sum_credit, line_count
            FROM je_stats
            WHERE line_count < 2 OR sum_debit IS DISTINCT FROM sum_credit
            ORDER BY company_id, ref
            """
        )
    ).fetchall()

    if rows:
        # Surface in alembic output so the operator sees it — Cli &
        # CI logs both stream stdout from the migration.
        print(
            f"WARNING: 0101_je_balance_trigger — {len(rows)} pre-existing "
            f"POSTED entries violate the new balance/line-count rule. "
            f"They are NOT retroactively rejected; future writes are. "
            f"First few:"
        )
        for row in rows[:10]:
            print(
                f"  id={row.id} ref={row.ref!r} company={row.company_id} "
                f"D={row.sum_debit} C={row.sum_credit} lines={row.line_count}"
            )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_je_balance_jl ON journal_lines"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_je_balance_je ON journal_entries"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS trg_je_balance_after_jl_change()"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS trg_je_balance_after_je_change()"
        )
    )
    op.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {_FN_NAME}(uuid)")
    )
