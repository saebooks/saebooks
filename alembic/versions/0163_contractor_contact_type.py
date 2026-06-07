"""0163_contractor_contact_type — add CONTRACTOR to contact_type_enum.

Why this migration exists
-------------------------
Richard reviewed his books and asked for contractors and suppliers to be
two distinct contact kinds: "who we buy goods/materials from" (SUPPLIER)
vs "who we pay to do work" (CONTRACTOR / sub-contractor / labour hire).
This adds a new value, CONTRACTOR, to the existing Postgres enum type
contact_type_enum (currently CUSTOMER / SUPPLIER / BOTH / BENEFICIARY).

CONTRACTOR is payable exactly like a SUPPLIER — the engine's bills /
expenses / payments / pay-run / ABA paths do NOT gate eligible payees by
contact_type (verified), so a CONTRACTOR contact can be billed/paid
with no further engine change. TPAR inclusion remains driven by the
explicit contacts.is_tpar_supplier flag (services/tpar.py), NOT by
this type; see the PR / tpar_decision for the recommended app-side default
(default is_tpar_supplier=True when contact_type=CONTRACTOR).

PostgreSQL quirk
----------------
ALTER TYPE ... ADD VALUE cannot run inside a transaction block that
later USES the new value, and Alembic wraps each migration in a
transaction. We therefore run it inside op.get_context().autocommit_block()
so it commits OUTSIDE the migration's transaction (PG 12+). IF NOT EXISTS
makes it idempotent (safe to re-run; no error if CONTRACTOR already present).

⚠ PARALLEL-MIGRATION FLAG — READ BEFORE MERGE
---------------------------------------------
This revision branches from 0161_je_engine_guard (the current
origin/main alembic head). At the time it was written there were TWO other,
UNMERGED 0162 migrations also branching from 0161:

  * PR #30 — 0162_je_guard_fixes
  * branch feat/invoice-written-off — 0162_je_guard_require_source

If any of those land before this one, the alembic chain will have MULTIPLE
HEADS. At merge time, whichever of {this, the 0162s} lands LAST must be
rebased so there is a SINGLE head: re-point this file's down_revision
onto the final 0162 head and RENUMBER this revision to 0164
(0164_contractor_contact_type). Do not merge with multiple heads.

Revision ID: 0163_contractor_contact_type
Revises:     0161_je_engine_guard
"""
from __future__ import annotations

from alembic import op

revision: str = "0163_contractor_contact_type"
down_revision: str | None = "0161_je_engine_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE must commit outside the migration txn (PG 12+).
    # IF NOT EXISTS keeps it idempotent.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE contact_type_enum ADD VALUE IF NOT EXISTS 'CONTRACTOR'"
        )


def downgrade() -> None:
    # No-op by design. PostgreSQL cannot DROP a value from an enum type, so
    # enum values are forward-only. Existing rows are untouched on downgrade;
    # 'CONTRACTOR' simply remains an (unused) member of contact_type_enum.
    # If you truly needed it gone you would have to recreate the type and
    # rewrite the column — out of scope for a reversible downgrade.
    pass
