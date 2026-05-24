"""RLS on pay_runs + pay_run_lines, drop foot-gun tenant_id default.

Three problems in 0090_pay_run_tables are fixed here:

1. ``pay_runs`` has no ENABLE / FORCE ROW LEVEL SECURITY and no
   tenant_isolation policy — table-owner role and any bypass-RLS role
   can enumerate all tenants' pay runs.

2. ``pay_run_lines`` has no tenant_id column at all; isolation depends
   entirely on the FK chain (pay_run_lines → pay_runs.tenant_id).
   That is insufficient once an app-layer helper queries pay_run_lines
   directly (e.g. for an employee portal view), because there is no
   column for the GUC predicate to match against.

3. ``pay_runs.tenant_id`` carries a server_default of
   '00000000-0000-0000-0000-000000000001'.  Any INSERT that omits
   tenant_id silently routes to the hard-coded dev tenant instead of
   raising an error at the DB layer.  The same class of foot-gun was
   removed from journal_entries in 0127_drop_journal_tenant_default —
   this migration applies the same fix here.

Fixes applied
-------------
A. ENABLE + FORCE ROW LEVEL SECURITY on pay_runs.
B. DROP / CREATE tenant_isolation policy on pay_runs matching the
   Class-A predicate (WITH CHECK present — see 0086 / 0083).
C. Add tenant_id UUID NOT NULL to pay_run_lines, backfilled from
   parent pay_runs row.
D. Add FK tenant_id → tenants.id ON DELETE RESTRICT on pay_run_lines.
E. ENABLE + FORCE ROW LEVEL SECURITY on pay_run_lines.
F. DROP / CREATE tenant_isolation policy on pay_run_lines.
G. DROP DEFAULT on pay_runs.tenant_id.

Backfill note (step C)
-----------------------
The column is added as nullable first, populated via a single UPDATE
that joins back to pay_runs, then altered to NOT NULL.  This avoids
an ERROR for any existing rows (there should be none in prod since the
payroll feature is not yet launched, but the pattern is safe either
way).

Reversibility
-------------
downgrade() is the exact reverse of upgrade(): re-add the default,
drop policies, disable RLS, drop the FK + column.  Idempotent via
DROP POLICY IF EXISTS.

Revision ID: 0128_pay_runs_rls
Revises: 0127_drop_journal_tenant_default
Create Date: 2026-05-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0128_pay_runs_rls"
down_revision: str | None = "0127_drop_journal_tenant_default"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Class-A predicate — byte-identical to migrations 0083, 0085, 0086.
_TENANT_PRED = (
    "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # A + B — pay_runs RLS
    # ------------------------------------------------------------------
    op.execute(sa.text("ALTER TABLE pay_runs ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE pay_runs FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON pay_runs"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON pay_runs "
            f"FOR ALL USING {_TENANT_PRED} WITH CHECK {_TENANT_PRED}"
        )
    )

    # ------------------------------------------------------------------
    # C — Add tenant_id to pay_run_lines (nullable first for backfill)
    # ------------------------------------------------------------------
    op.add_column(
        "pay_run_lines",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )

    # Backfill from parent pay_run row.
    op.execute(
        sa.text(
            "UPDATE pay_run_lines prl "
            "SET tenant_id = pr.tenant_id "
            "FROM pay_runs pr "
            "WHERE prl.pay_run_id = pr.id"
        )
    )

    # Now enforce NOT NULL — will fail fast if any orphaned lines exist.
    op.alter_column(
        "pay_run_lines",
        "tenant_id",
        nullable=False,
    )

    # ------------------------------------------------------------------
    # D — FK tenant_id → tenants.id ON DELETE RESTRICT
    # ------------------------------------------------------------------
    op.create_foreign_key(
        "fk_pay_run_lines_tenant_id",
        "pay_run_lines",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_pay_run_lines_tenant_id",
        "pay_run_lines",
        ["tenant_id"],
    )

    # ------------------------------------------------------------------
    # E + F — pay_run_lines RLS
    # ------------------------------------------------------------------
    op.execute(sa.text("ALTER TABLE pay_run_lines ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE pay_run_lines FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON pay_run_lines"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON pay_run_lines "
            f"FOR ALL USING {_TENANT_PRED} WITH CHECK {_TENANT_PRED}"
        )
    )

    # ------------------------------------------------------------------
    # G — drop the foot-gun server_default on pay_runs.tenant_id
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            "ALTER TABLE pay_runs ALTER COLUMN tenant_id DROP DEFAULT"
        )
    )


def downgrade() -> None:
    # Restore the foot-gun default (symmetric reversal).
    op.execute(
        sa.text(
            "ALTER TABLE pay_runs ALTER COLUMN tenant_id "
            "SET DEFAULT '00000000-0000-0000-0000-000000000001'::uuid"
        )
    )

    # Drop pay_run_lines RLS.
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON pay_run_lines"))
    op.execute(sa.text("ALTER TABLE pay_run_lines NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE pay_run_lines DISABLE ROW LEVEL SECURITY"))

    # Drop the tenant_id FK, index, and column from pay_run_lines.
    op.drop_constraint("fk_pay_run_lines_tenant_id", "pay_run_lines", type_="foreignkey")
    op.drop_index("ix_pay_run_lines_tenant_id", table_name="pay_run_lines")
    op.drop_column("pay_run_lines", "tenant_id")

    # Drop pay_runs RLS.
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON pay_runs"))
    op.execute(sa.text("ALTER TABLE pay_runs NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE pay_runs DISABLE ROW LEVEL SECURITY"))
