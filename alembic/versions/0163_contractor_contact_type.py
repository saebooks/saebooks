"""0163_contractor_contact_type — add CONTRACTOR + SUB_CONTRACTOR to contact_type_enum.

Why this migration exists
-------------------------
Richard reviewed his books and asked for three distinct tiers of payee:
SUPPLIER (goods/materials), CONTRACTOR (higher-tier entity delivering a
whole section of work — spend is COST OF SALES), and SUB_CONTRACTOR
(middle-tier labour-services payee under a contractor — spend is EXPENSE).
This adds TWO new values to the existing Postgres enum type
contact_type_enum (previously CUSTOMER / SUPPLIER / BOTH / BENEFICIARY):

  * CONTRACTOR     — higher-tier entity engaged to deliver a whole section
                     of a job. Spend is COST OF SALES (recommend 5-2000
                     Contractor Costs, sibling to 5-1000 Materials Supplied).
                     NOT TPAR-reportable: ATO "labour incidental to the
                     supply of materials" exemption. NOTE — the exemption
                     applies because Richard's contractors supply materials +
                     incidental labour; it is NOT because the payee is a
                     company. Default is_tpar_supplier=False.
  * SUB_CONTRACTOR — middle-tier labour-services payee under a contractor.
                     Spend is OVERHEAD EXPENSE. TPAR-reportable: the app
                     should default is_tpar_supplier=True for SUB_CONTRACTOR
                     (engine flag is the source of truth; no type→TPAR
                     coupling in engine code).

Both are payable exactly like a SUPPLIER — the engine's bills / expenses /
payments / pay-run / ABA paths do NOT gate eligible payees by contact_type
(verified), so either contact can be billed/paid with no further engine
change. TPAR inclusion remains driven SOLELY by the explicit
contacts.is_tpar_supplier flag (services/tpar.py), NEVER by contact_type.

PostgreSQL quirk
----------------
ALTER TYPE ... ADD VALUE cannot run inside a transaction block that
later USES the new value, and Alembic wraps each migration in a
transaction. We therefore run it inside op.get_context().autocommit_block()
so it commits OUTSIDE the migration's transaction (PG 12+). IF NOT EXISTS
makes each ADD VALUE idempotent (safe to re-run; no error if the value is
already present). Both values are added in the same autocommit_block.

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
        op.execute(
            "ALTER TYPE contact_type_enum ADD VALUE IF NOT EXISTS 'SUB_CONTRACTOR'"
        )


def downgrade() -> None:
    # No-op by design. PostgreSQL cannot DROP a value from an enum type, so
    # enum values are forward-only. Existing rows are untouched on downgrade;
    # 'CONTRACTOR' simply remains an (unused) member of contact_type_enum.
    # If you truly needed it gone you would have to recreate the type and
    # rewrite the column — out of scope for a reversible downgrade. Both
    # CONTRACTOR and SUB_CONTRACTOR simply remain (unused) members.
    pass
