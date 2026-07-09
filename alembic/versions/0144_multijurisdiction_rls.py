"""Enable RLS on the three multi-jurisdiction tables 0100 created without it.

Migration 0100 (multi-jurisdiction company engine) added ``tax_periods``,
``tax_returns`` and ``lodgement_records`` — each carries a ``tenant_id``
NOT NULL column with an FK to ``tenants(id)`` and inherits ``CompanyScoped``
— but it never wrapped them in the ``tenant_isolation`` RLS policy that
0055/0083/0085 install on every other customer-data table. The db-rls audit
graph flagged all three as ``incomplete`` (tenant_id present, RLS absent).

This closes that gap with the standard Class-A treatment (direct
``tenant_id`` predicate), identical in shape to 0083:

* ``ENABLE ROW LEVEL SECURITY``
* ``FORCE ROW LEVEL SECURITY`` — without FORCE the table owner bypasses
  the policy (the lesson 0055 documented and 0083 repeats).
* ``CREATE POLICY tenant_isolation ... FOR ALL USING <pred> WITH CHECK
  <pred>`` — predicate reused verbatim so the whole DB shares one
  definition of "tenant scope".

The three tables are empty in every tenant today (the multi-jurisdiction
feature is pre-alpha scaffolding), so this is a pure structural fix with no
data backfill and no behavioural risk to live code paths.

Reversibility: ``downgrade()`` drops the policy, ``NO FORCE``, ``DISABLE
ROW LEVEL SECURITY``. Idempotent — each step uses IF EXISTS / is a no-op on
an already-applied state so a partial previous attempt does not block a
re-run (matches 0083).

Revision ID: 0133_multijurisdiction_rls
Revises: 0132_gst_system_managed_backfill
Create Date: 2026-06-01
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0144_multijurisdiction_rls"
down_revision: str | None = "0143_account_credit_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The three tables migration 0100 created without the tenant_isolation
# policy. All three already carry tenant_id NOT NULL + FK to tenants.
_TABLES: tuple[str, ...] = (
    "tax_periods",
    "tax_returns",
    "lodgement_records",
)

# Reuse 0055/0083's predicate verbatim — one policy shape across the DB.
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING


def upgrade() -> None:
    for table in _TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
            )
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
