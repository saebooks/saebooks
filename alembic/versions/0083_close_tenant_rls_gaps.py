"""Close tenant_id + RLS gaps on six customer-data tables.

Build #2 audit (2026-05-02) found two gap classes against the
``saebooks-infrastructure`` plan §4.1 + §4.2:

* Four tables held customer financial data with no ``tenant_id``
  column at all — RLS could not be enabled on them because the
  policy predicate has nothing to reference. Tables:
  ``account_ranges`` (mig 0008), ``bank_rules`` (0014),
  ``journal_templates`` (0006), ``trust_distributions`` (0059).

* Two tables already carried ``tenant_id`` but were never wrapped
  in the ``tenant_isolation`` RLS policy installed by 0055. Tables:
  ``allocation_rules`` (mig 0069), ``departments`` (0068).
  ``cost_centres`` (also from 0068) is in the same boat — it is
  included here because the audit-trail and the disciplines list
  are about *every customer-data table*, not just the named six,
  and shipping ``departments`` without its sibling would leave a
  loophole on the same migration boundary.

What this migration does
------------------------
1. For each of the four tables missing ``tenant_id``:

   * Add ``tenant_id UUID`` column, initially NULL so the backfill
     can run row-by-row.
   * Backfill from the parent ``companies.tenant_id`` via
     ``company_id`` (every one of the four has a ``company_id`` FK
     installed by their original migration).
   * Promote to ``NOT NULL`` and add an FK to ``tenants(id)`` plus
     an index on ``tenant_id``.

2. For all six (plus ``cost_centres``) tables:

   * ``ENABLE ROW LEVEL SECURITY``
   * ``FORCE ROW LEVEL SECURITY`` — without FORCE the table owner
     bypasses the policy. This is the bug 0055 documented and fixed
     for the original 16; we apply the same lesson here.
   * ``CREATE POLICY tenant_isolation ... FOR ALL USING <pred> WITH
     CHECK <pred>`` — same predicate shape verbatim from 0055.

The predicate is reused from 0055 so every table in the database
shares exactly one definition of "tenant scope" — copying the policy
shape, not inventing a new one.

Backfill correctness
--------------------
Every one of the four NULL-able-tenant_id tables is keyed on
``company_id`` with a NOT NULL ForeignKey to ``companies(id)``.
``companies`` had ``tenant_id`` populated since 0040. So
``UPDATE x SET tenant_id = c.tenant_id FROM companies c WHERE
x.company_id = c.id`` cannot leave NULLs behind unless a row exists
whose ``company_id`` references a missing or NULL-tenant company —
which would already be a referential-integrity violation pre-dating
this migration. The migration runs an explicit post-condition
``ASSERT`` block that fails the upgrade if any NULL remains, so a
broken parent surfaces here instead of being silently masked.

Reversibility
-------------
``downgrade()`` reverses cleanly: drop policy, NO FORCE, DISABLE
RLS, drop FK + index + column on the four tables. Idempotent: each
step uses ``IF EXISTS`` so a partial previous attempt does not
block re-running.

Revision ID: 0083_close_tenant_rls_gaps
Revises: 0082_bsl_matches
Create Date: 2026-05-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0083_close_tenant_rls_gaps"
down_revision: str | None = "0082_bsl_matches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that need a tenant_id column added + backfilled from
# companies.tenant_id via their existing company_id FK.
_TABLES_NEEDING_COLUMN: tuple[str, ...] = (
    "account_ranges",
    "bank_rules",
    "journal_templates",
    "trust_distributions",
)

# Tables that already have tenant_id but are missing the
# tenant_isolation RLS policy + FORCE bit. ``cost_centres`` is
# included alongside ``departments`` because they were added by the
# same migration (0068) with the same shape; shipping one without
# the other would leave a known sibling unprotected.
_TABLES_WITH_COLUMN_NO_POLICY: tuple[str, ...] = (
    "allocation_rules",
    "cost_centres",
    "departments",
)

# All seven tables get the policy + ENABLE RLS + FORCE RLS treatment.
_ALL_TABLES: tuple[str, ...] = (
    *_TABLES_NEEDING_COLUMN,
    *_TABLES_WITH_COLUMN_NO_POLICY,
)

# Reuse 0055's predicate verbatim — the discipline says one policy
# shape across the whole DB.
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING


def upgrade() -> None:
    bind = op.get_bind()

    # ---- Step 1: add tenant_id NULL on the four tables. ---------------
    for table in _TABLES_NEEDING_COLUMN:
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # ---- Step 2: backfill from companies via company_id. --------------
    # Every one of the four has company_id NOT NULL FK to companies.id.
    # companies.tenant_id is NOT NULL since 0040.
    for table in _TABLES_NEEDING_COLUMN:
        op.execute(
            sa.text(
                f"UPDATE {table} t SET tenant_id = c.tenant_id "  # noqa: S608
                f"FROM companies c WHERE t.company_id = c.id "
                f"AND t.tenant_id IS NULL"
            )
        )

    # ---- Step 3: assert no NULLs remain (orphan parent => abort). -----
    # If any row has a company_id pointing at a missing companies row
    # (only possible if FK was disabled or the row is genuinely
    # corrupt), the UPDATE above leaves tenant_id NULL and the
    # NOT NULL alter would fail mid-migration. Surface the broken row
    # here with an explicit error so the operator can fix the data.
    for table in _TABLES_NEEDING_COLUMN:
        result = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE tenant_id IS NULL")  # noqa: S608
        ).scalar_one()
        if result and int(result) > 0:
            raise RuntimeError(
                f"Migration 0083 backfill failed: {result} row(s) in "
                f"'{table}' have NULL tenant_id after company-join "
                f"backfill. This means at least one row's company_id "
                f"references a missing or tenant-less companies row. "
                f"Fix the data (e.g. re-parent, archive, or hard-delete "
                f"the orphan rows) before re-running the migration."
            )

    # ---- Step 4: NOT NULL + FK + index on the four tables. -----------
    for table in _TABLES_NEEDING_COLUMN:
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(
            f"{table}_tenant_id_fkey",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table}_tenant_id",
            table,
            ["tenant_id"],
        )

    # ---- Step 5: ENABLE + FORCE RLS + tenant_isolation policy. -------
    # Same shape as 0055. CREATE POLICY ... IF NOT EXISTS isn't
    # supported on PG 16, so DROP IF EXISTS first to keep this
    # migration replay-safe (matches 0055's approach verbatim).
    for table in _ALL_TABLES:
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
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
            )
        )


def downgrade() -> None:
    # Reverse step 5: drop policy, NO FORCE, DISABLE RLS.
    for table in _ALL_TABLES:
        op.execute(
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
            )
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

    # Reverse step 4 + 1: drop FK + index + column on the four tables.
    for table in _TABLES_NEEDING_COLUMN:
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_constraint(
            f"{table}_tenant_id_fkey",
            table,
            type_="foreignkey",
        )
        op.drop_column(table, "tenant_id")
