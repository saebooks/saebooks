"""Close remaining tenant RLS gaps — 8 tables (3 direct tenant_id, 5 via companies FK).

Why this migration exists
-------------------------
Migration 0083 closed seven specific tables but a parallel audit during
the 0084 work (``feat/rls-cli-app-role``) found more. ``bank_feed_accounts``
in particular had ``relrowsecurity=f, relforcerowsecurity=f`` — a
``saebooks_app`` (NOBYPASSRLS) caller could read every tenant's feed
accounts. The 0084 SECURITY DEFINER enumerator dodges this by joining
to ``companies`` (which IS RLS-protected) and pulling ``tenant_id``
from there, but the underlying table-level gap remained.

Two gap classes
---------------
After 0083+0084 there are eight tables in the saebooks DB with a
tenant or company FK that have never had RLS applied:

* **Class A — has its own ``tenant_id`` column** (3 tables). Same
  policy shape as 0055/0083: ``tenant_id = current_setting(...)``.

  - ``audit_log`` (mig 0076) — admin hard-delete forensics; tenant-keyed.
  - ``bsl_matches`` (mig 0082) — N:1 bank-line reconciliation junction.
  - ``idempotency_records`` (mig 0046) — request-replay guard, keyed
    by tenant.

* **Class B — has only ``company_id``** (5 tables). Per the operator
  brief, do NOT add a redundant ``tenant_id`` column to these — derive
  tenant scope through the FK to ``companies``. The policy predicate
  uses a subquery against ``companies``:

      USING (company_id IN
             (SELECT id FROM companies
              WHERE tenant_id = current_setting('app.current_tenant', true)::uuid))

  ``companies`` itself has FORCE RLS + ``tenant_isolation`` (since
  0055), so under the ``saebooks_app`` role the inner SELECT is
  scoped to the current tenant — the outer policy collapses to "is
  this row's company one of mine?". Under the BYPASSRLS owner role
  (used by migrations + the SECDEF enumerator) the inner SELECT
  returns every company and the predicate is trivially satisfied —
  exactly the behaviour the SECDEF function relies on.

  - ``bank_feed_accounts`` (mig 0029) — the original target.
  - ``bank_feed_clients`` (mig 0029) — sibling, same FK shape.
  - ``ato_sbr_configs`` (mig 0050) — STP/BAS keystore per company.
  - ``document_counters`` (mig 0033) — invoice/bill numbering.
  - ``period_locks`` (mig 0011) — close-the-books guard.

Predicate choice — why not add tenant_id?
-----------------------------------------
The plan brief explicitly forbids adding ``tenant_id`` columns to
existing tables. The FK-subquery pattern is strictly weaker than a
direct tenant_id column for the planner (it can't push the predicate
into the index seek) but the candidate tables are tiny:

* ``period_locks`` — 141 rows in prod, ``UNIQUE(company_id)``-ish.
* ``document_counters`` — 13 rows.
* ``bank_feed_*`` — 0 rows in prod (feeds not yet onboarded).
* ``ato_sbr_configs`` — 0 rows.

Performance is not a concern at these sizes, and the consistency win
(every per-company table reaches tenant scope through the same
``companies`` join) is worth the small planner cost.

Why FOR ALL with WITH CHECK
---------------------------
Inserts/updates from a ``saebooks_app`` session must be blocked from
writing a ``company_id`` belonging to another tenant. The ``WITH
CHECK`` clause uses the same predicate so a forged write is rejected
at the policy level, not just hidden on the next read.

Effect on 0084's SECDEF function
--------------------------------
``bank_feeds_active_accounts_for_sync()`` is owned by ``saebooks``
(BYPASSRLS=t). The function body has no GUC dependency. RLS does not
apply to BYPASSRLS roles regardless of FORCE — so the function still
returns every active feed account across every tenant after this
migration. Verified by the ``test_secdef_function_works_for_app_role``
case: the ``saebooks_app`` caller invokes the function and gets back
both seeded accounts even with no ``app.current_tenant`` set.

Reversibility
-------------
``downgrade()`` is symmetric: drop policy, NO FORCE, DISABLE RLS for
each table. Each step is idempotent (``DROP POLICY IF EXISTS``,
``DISABLE`` is a no-op when already disabled).

Revision ID: 0085_rls_remaining_gaps
Revises: 0084_bank_feeds_secdef_enum
Create Date: 2026-05-03
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0085_rls_remaining_gaps"
down_revision: str | None = "0084_bank_feeds_secdef_enum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Class A: tables with their own ``tenant_id`` column. Standard
# 0055-shaped policy.
_TABLES_WITH_TENANT_ID: tuple[str, ...] = (
    "audit_log",
    "bsl_matches",
    "idempotency_records",
)

# Class B: tables with only ``company_id``; we route the policy
# through a subquery on ``companies``. Order chosen alphabetically
# for stable migration output.
_TABLES_WITH_COMPANY_ID: tuple[str, ...] = (
    "ato_sbr_configs",
    "bank_feed_accounts",
    "bank_feed_clients",
    "document_counters",
    "period_locks",
)

# Predicate for Class A — byte-identical to migrations 0055 + 0083.
_TENANT_PRED = (
    "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
)

# Predicate for Class B — company_id must belong to a company in the
# current tenant. ``companies`` is already RLS-protected so under
# saebooks_app the inner SELECT is naturally tenant-scoped; under
# the BYPASSRLS owner the inner SELECT yields every company and the
# predicate is trivially true.
_COMPANY_PRED = (
    "(company_id IN ("
    "SELECT id FROM companies "
    "WHERE tenant_id = current_setting('app.current_tenant', true)::uuid"
    "))"
)


def _enable_force_policy(table: str, predicate: str) -> None:
    """Enable + force RLS, install ``tenant_isolation`` policy.

    Idempotent — DROP POLICY IF EXISTS first so re-running this
    migration after a partial failure is safe (matches 0055/0083).
    """
    op.execute(
        sa.text(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"  # noqa: S608
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"  # noqa: S608
        )
    )
    op.execute(
        sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    )
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"FOR ALL USING {predicate} WITH CHECK {predicate}"
        )
    )


def upgrade() -> None:
    for table in _TABLES_WITH_TENANT_ID:
        _enable_force_policy(table, _TENANT_PRED)

    for table in _TABLES_WITH_COMPANY_ID:
        _enable_force_policy(table, _COMPANY_PRED)


def downgrade() -> None:
    for table in (*_TABLES_WITH_TENANT_ID, *_TABLES_WITH_COMPANY_ID):
        op.execute(
            sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"  # noqa: S608
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"  # noqa: S608
            )
        )
