"""M2 — DB-level guards on payment_allocations.

Audit findings (CRITICAL #2 / #3): the application asserts that an
allocation row points at exactly one document and that the cumulative
allocation against any one document never exceeds its total — but
those assertions live in saebooks.services.payments and a write that
bypasses the service can leave the AR/AP control account
inconsistent with the underlying documents. Once a payment is
allocated past invoice.total the customer's open balance is wrong;
once a single allocation row carries both invoice_id and bill_id at
once, every aging report joins twice.

Two guards
----------
1. **XOR CHECK** — exactly one of (invoice_id, credit_note_id, bill_id)
   is non-null per allocation row. Standard CHECK constraint, fires
   immediately.

2. **Cumulative trigger** — for whichever target column is set on the
   row being touched, SUM(amount) over all rows pointing at the same
   document must not exceed that document's ``total``. CONSTRAINT
   TRIGGER DEFERRABLE INITIALLY DEFERRED so an "INSERT three rows
   each $50 against a $100 invoice + DELETE one of them" sequence
   inside one transaction doesn't trip the check on an intermediate
   state.

Direction note
--------------
The trigger compares against ``ABS(doc.total)`` and ``SUM(amount)``
without sign normalisation. Refunds (negative payments) currently
flow through OUTGOING payments with positive allocation amounts
against credit_notes; the magnitude check is correct in both signs.
If the data model later allows mixed-sign allocations on one doc the
trigger will need a refactor.

Existing data
-------------
The migration logs but does not retroactively reject violations,
matching the M1 / 0055 / 0056 pattern.

Reversibility
-------------
``downgrade()`` drops the trigger, function, and CHECK. Schema returns
to "any service can write any allocation".

Revision ID: 0102_alloc_invariants
Revises: 0101_je_balance_trigger
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0102_alloc_invariants"
down_revision: str | None = "0101_je_balance_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------------------------------------------------------- #
    # 1. XOR check — exactly one target FK per row.                    #
    # ---------------------------------------------------------------- #
    # Boolean arithmetic: number of non-null targets must equal 1.
    op.execute(
        sa.text(
            """
            ALTER TABLE payment_allocations
            ADD CONSTRAINT ck_payment_allocations_xor_target
            CHECK (
                (CASE WHEN invoice_id     IS NOT NULL THEN 1 ELSE 0 END) +
                (CASE WHEN credit_note_id IS NOT NULL THEN 1 ELSE 0 END) +
                (CASE WHEN bill_id        IS NOT NULL THEN 1 ELSE 0 END)
                = 1
            )
            """
        )
    )

    # ---------------------------------------------------------------- #
    # 2. Cumulative-allocation trigger.                                #
    # ---------------------------------------------------------------- #
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION check_allocation_cap(
                p_invoice_id     uuid,
                p_credit_note_id uuid,
                p_bill_id        uuid
            ) RETURNS void AS $$
            DECLARE
                v_total      numeric;
                v_alloc_sum  numeric;
                v_doc_kind   text;
                v_doc_id     uuid;
            BEGIN
                IF p_invoice_id IS NOT NULL THEN
                    v_doc_kind := 'invoice';
                    v_doc_id := p_invoice_id;
                    SELECT total INTO v_total FROM invoices WHERE id = p_invoice_id;
                ELSIF p_credit_note_id IS NOT NULL THEN
                    v_doc_kind := 'credit_note';
                    v_doc_id := p_credit_note_id;
                    SELECT total INTO v_total FROM credit_notes WHERE id = p_credit_note_id;
                ELSIF p_bill_id IS NOT NULL THEN
                    v_doc_kind := 'bill';
                    v_doc_id := p_bill_id;
                    SELECT total INTO v_total FROM bills WHERE id = p_bill_id;
                ELSE
                    -- Should be unreachable thanks to the XOR CHECK,
                    -- but the trigger may fire from a DELETE where the
                    -- pre-image is gone — bail out cleanly.
                    RETURN;
                END IF;

                -- Document might have been deleted in the same tx;
                -- nothing to enforce.
                IF v_total IS NULL THEN
                    RETURN;
                END IF;

                SELECT COALESCE(SUM(amount), 0) INTO v_alloc_sum
                FROM payment_allocations
                WHERE COALESCE(invoice_id, credit_note_id, bill_id) = v_doc_id;

                IF ABS(v_alloc_sum) > ABS(v_total) THEN
                    RAISE EXCEPTION
                        'Cumulative allocation against %s % exceeds total: '
                        'allocated=%, total=%',
                        v_doc_kind, v_doc_id, v_alloc_sum, v_total
                        USING ERRCODE = 'check_violation';
                END IF;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION trg_allocation_cap()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    PERFORM check_allocation_cap(
                        OLD.invoice_id, OLD.credit_note_id, OLD.bill_id
                    );
                ELSE
                    PERFORM check_allocation_cap(
                        NEW.invoice_id, NEW.credit_note_id, NEW.bill_id
                    );
                    -- An UPDATE that re-points the row at a different
                    -- doc must validate the OLD doc too — its alloc_sum
                    -- has changed.
                    IF TG_OP = 'UPDATE' AND (
                        OLD.invoice_id     IS DISTINCT FROM NEW.invoice_id     OR
                        OLD.credit_note_id IS DISTINCT FROM NEW.credit_note_id OR
                        OLD.bill_id        IS DISTINCT FROM NEW.bill_id
                    ) THEN
                        PERFORM check_allocation_cap(
                            OLD.invoice_id, OLD.credit_note_id, OLD.bill_id
                        );
                    END IF;
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
            CREATE CONSTRAINT TRIGGER trg_allocation_cap
            AFTER INSERT OR UPDATE OR DELETE ON payment_allocations
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION trg_allocation_cap();
            """
        )
    )

    # ---------------------------------------------------------------- #
    # 3. Log pre-existing violations (XOR + cap).                      #
    # ---------------------------------------------------------------- #
    xor_rows = bind.execute(
        sa.text(
            """
            SELECT id, payment_id, invoice_id, credit_note_id, bill_id
            FROM payment_allocations
            WHERE
                (CASE WHEN invoice_id     IS NOT NULL THEN 1 ELSE 0 END) +
                (CASE WHEN credit_note_id IS NOT NULL THEN 1 ELSE 0 END) +
                (CASE WHEN bill_id        IS NOT NULL THEN 1 ELSE 0 END)
                <> 1
            """
        )
    ).fetchall()

    if xor_rows:
        # Pre-existing rows that violate the new CHECK will block the
        # ALTER TABLE itself — surface them clearly. (If we hit this,
        # the migration has already failed above; the print is a
        # courtesy for diagnostics.)
        print(
            f"WARNING: 0102 — {len(xor_rows)} payment_allocations rows "
            f"violate the new XOR CHECK and will block the ALTER TABLE. "
            f"First few: {[dict(r._mapping) for r in xor_rows[:5]]}"
        )

    cap_rows = bind.execute(
        sa.text(
            """
            WITH per_doc AS (
                SELECT
                    COALESCE(invoice_id, credit_note_id, bill_id) AS doc_id,
                    SUM(amount) AS alloc_sum
                FROM payment_allocations
                GROUP BY 1
            ),
            doc_totals AS (
                SELECT id AS doc_id, total FROM invoices
                UNION ALL
                SELECT id, total FROM credit_notes
                UNION ALL
                SELECT id, total FROM bills
            )
            SELECT pd.doc_id, pd.alloc_sum, dt.total
            FROM per_doc pd
            JOIN doc_totals dt USING (doc_id)
            WHERE ABS(pd.alloc_sum) > ABS(dt.total)
            """
        )
    ).fetchall()

    if cap_rows:
        print(
            f"WARNING: 0102 — {len(cap_rows)} document(s) have cumulative "
            f"allocations exceeding their total. NOT retroactively "
            f"rejected; future writes are. First few:"
        )
        for row in cap_rows[:10]:
            print(
                f"  doc_id={row.doc_id} allocated={row.alloc_sum} total={row.total}"
            )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_allocation_cap ON payment_allocations"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS trg_allocation_cap()"))
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS check_allocation_cap(uuid, uuid, uuid)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE payment_allocations "
            "DROP CONSTRAINT IF EXISTS ck_payment_allocations_xor_target"
        )
    )
