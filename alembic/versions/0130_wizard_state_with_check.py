"""Add WITH CHECK to wizard_state tenant_isolation policy.

0089_wizard_state.py created the ``wizard_state_tenant_isolation``
policy with a USING clause only::

    CREATE POLICY wizard_state_tenant_isolation ON wizard_state
        USING (tenant_id = current_setting('app.current_tenant')::uuid)

A policy without WITH CHECK permits any INSERT or UPDATE whose
``tenant_id`` value differs from the GUC, because Postgres only
applies the USING filter for SELECT/DELETE commands when no WITH CHECK
clause is present — not for INSERT or UPDATE rows.  The result is that
a caller who can set ``app.current_tenant`` to tenant A and then
INSERT a wizard_state row with ``tenant_id = tenant_B_uuid`` will
succeed at the DB layer.

0091_wizard_state_force_rls later added FORCE to prevent the
schema-owner role from bypassing the policy entirely, but it did not
fix the missing WITH CHECK — this migration finishes the job.

The fix is a DROP + CREATE (Postgres does not support ALTER POLICY
for the predicate expressions, only the ROLE target list).

Note: 0089 omits the ``true`` sentinel from current_setting() whereas
0086 (bank_feed_external_creds) uses ``current_setting(..., true)``.
The second argument makes the call return NULL rather than raising an
error when the GUC is not set.  Aligning wizard_state to the safer
form (with ``true``) while recreating the policy.

Revision ID: 0129_wizard_state_with_check
Revises: 0128_pay_runs_rls
Create Date: 2026-05-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0130_wizard_state_with_check"
down_revision: str | None = "0129_pay_runs_rls"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TENANT_PRED = (
    "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
)


def upgrade() -> None:
    op.execute(
        sa.text("DROP POLICY IF EXISTS wizard_state_tenant_isolation ON wizard_state")
    )
    op.execute(
        sa.text(
            "CREATE POLICY wizard_state_tenant_isolation ON wizard_state "
            f"FOR ALL USING {_TENANT_PRED} WITH CHECK {_TENANT_PRED}"
        )
    )


def downgrade() -> None:
    # Restore the original (WITH CHECK-less) form from 0089.
    op.execute(
        sa.text("DROP POLICY IF EXISTS wizard_state_tenant_isolation ON wizard_state")
    )
    op.execute(
        sa.text(
            "CREATE POLICY wizard_state_tenant_isolation ON wizard_state "
            "USING (tenant_id = current_setting('app.current_tenant')::uuid)"
        )
    )
