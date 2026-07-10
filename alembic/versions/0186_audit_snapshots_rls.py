"""audit_snapshots — tenant_id + FORCE RLS remediation (Wave C).

Background
----------
Migration 0011 created ``audit_snapshots`` with no tenant scoping.
0055 (the P0 cross-tenant-leak fix) explicitly carved it out of scope:
"row-id-keyed and only reachable via a tenant-scoped parent lookup" —
true at the time, because nothing read the table directly; every
caller looked up a specific ``(table_name, row_id)`` pair after
already resolving the parent through a tenant-scoped query. That
assumption breaks the moment a direct browse API exists (Wave C
Module 2 adds one, gated by ``FLAG_AUDIT_SNAPSHOTS``) — a cross-tenant
row-id guess would otherwise return another tenant's before/after
financial data. This migration closes that gap BEFORE the browse
route ships (both land in the same wave / same PR, never one without
the other).

What this migration does
-------------------------
1. Add ``tenant_id UUID`` (nullable — see "Why nullable" below) + FK
   to ``tenants(id)`` ON DELETE RESTRICT + an index.
2. Backfill existing rows in two passes (see "Backfill strategy").
3. ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` and an ASYMMETRIC
   ``tenant_isolation`` policy (see "Why asymmetric" below) —
   deliberately NOT the verbatim standard-shape policy the RLS
   checklist names, for a specific, load-bearing reason.
4. No GRANT step: ``audit_snapshots`` predates migration 0056's
   blanket ``saebooks_app`` privilege grant (0011 < 0056), so the
   table's base DML privileges are already correct; only the new
   column needs anything, and GRANT is table-level, not column-level.

Backfill strategy
------------------
Two passes, run in order, each only touching rows still NULL:

**Pass 1 — direct from the JSON.** Every real ``audit_svc`` capture
call site (``accounts``/``contacts``/``bank_rules``/``items``/
``journal``/``projects``/``tax_codes`` — the "8 services" the Wave C
brief names; ``exports/company.py`` turned out on inspection to be a
CSV *reader*, not a writer, and doesn't belong in that count) snapshots
a ``CompanyScoped`` ORM row via ``audit.capture()``/``_row_to_dict``,
which serialises EVERY column — including ``tenant_id`` when the model
has one. So for any table whose model carries its own ``tenant_id``
(accounts, account_ranges, bank_rules, bank_statement_lines,
companies, contacts, journal_entries, journal_templates, period_locks,
tax_codes, items, projects — every CompanyScoped table this codebase
has ever audited), the value is sitting in ``before_data``/
``after_data`` as plain JSON, independent of whether the live parent
row still exists. This is MORE robust than joining to the current
table (survives a since-deleted parent) and is the primary source.

**Pass 2 — ``journal_lines`` fallback.** ``JournalLine`` is a child
table with no ``tenant_id`` column of its own (0055's carve-out), so
pass 1 can't see it. But every ``journal_lines`` snapshot in this
codebase (``services/journal.py``'s ``delete()``, the cascade-delete
path — the only call site that ever snapshots a line directly; a
normal line-replacing edit snapshots the parent ``JournalEntry`` row,
already covered by pass 1) is written in lockstep with a sibling
``journal_entries`` snapshot for the SAME parent, in the SAME
transaction, immediately before the line snapshot. Pass 2 self-joins
``audit_snapshots`` back onto itself: for each still-NULL
``journal_lines`` row, find the ``journal_entries``-typed snapshot
whose ``row_id`` equals this line's ``before_data``/``after_data``
``entry_id``, and copy ITS ``tenant_id`` (already resolved by pass 1).
This works even though the live ``journal_entries`` row is gone too
(the whole point of ``delete()`` is that both rows are being removed)
because the join target is the permanent snapshot record, not the
live table.

**Remainder — left NULL, not guessed.** The ``settings`` table has no
tenant column anywhere (it is architecturally global — ironically the
same "global, not tenant-scoped" property that was Module 1's root
bug for ``audit_mode``) and ``services/settings.py`` writes its
snapshot via a raw ``AuditSnapshot(...)`` construction, not through
``audit.snapshot()``/``snapshot_row()``, so it never had a tenant_id
to serialise in the first place. Per the Wave C brief's explicit
instruction — "for rows whose tenant is genuinely underivable, DO NOT
guess" — these stay NULL. Any OTHER row that stays NULL after both
passes (e.g. a table_name/row_id this migration's author didn't
anticipate, or an orphaned parent from data predating full tenant
rollout) is likewise left alone rather than defaulted to a guessed
tenant. **This migration was NOT run against a copy of production
data while being built** (by policy — see the Wave C report for why),
so the exact post-backfill NULL count is unknown at merge time; the
``RAISE NOTICE`` at the end of ``upgrade()`` prints it (and a
breakdown by ``table_name``) into the migration log the first time
this runs for real, so it's visible to whoever applies it, not
silently swallowed.

Why nullable (not NOT NULL)
-----------------------------
The Wave C brief is explicit: "Add `tenant_id NOT NULL` only if you
can guarantee the backfill covers all rows; otherwise stage it." Given
the known-underivable ``settings`` category above, that guarantee does
not hold — so this migration stops short of NOT NULL. A NULL
``tenant_id`` row is not a data-integrity gap: the RLS policy below
makes it fail CLOSED (invisible under every tenant context), which is
the safe direction to fail in.

Why the ``tenant_isolation`` policy is ASYMMETRIC (deviation, flagged)
-------------------------------------------------------------------------
The RLS checklist / prior migrations (0083, 0085) use ONE predicate for
both ``USING`` and ``WITH CHECK``:
``tenant_id = current_setting('app.current_tenant', true)::uuid``.
Copying that verbatim here would BREAK PRODUCTION the moment this
ships: ``services/settings.py``'s ``set()`` inserts an
``AuditSnapshot`` row with ``tenant_id`` always NULL (global table, no
tenant context to stamp) via the ``saebooks_app`` runtime role, which
is FORCE-RLS-bound with no BYPASSRLS. A NULL-vs-anything comparison is
never TRUE in SQL, so a strict ``WITH CHECK`` would silently turn
every settings write into an RLS-violation 500 — a real, live code
path (every ``PATCH`` that touches a setting, including this very
wave's own ``audit_mode`` company-level migration path used
historically) breaks, not a theoretical one.

This migration uses:
  ``USING (tenant_id = current_setting('app.current_tenant', true)::uuid)``
  ``WITH CHECK (tenant_id IS NULL OR tenant_id = current_setting('app.current_tenant', true)::uuid)``

i.e. reads stay STRICT (a NULL-tenant row is invisible to every
tenant, matching the "fail closed" design above), but writes are
PERMISSIVE for the NULL case (a legitimate tenant-less capture can
still be inserted) while still refusing a write that claims to belong
to a DIFFERENT tenant than the caller's own session GUC. This is a
deliberate, reasoned deviation from the standard verbatim policy
shape — flagged here and in the Wave C report for review, rather than
copied blind and shipped as a landmine.

Reversibility
-------------
``downgrade()`` reverses cleanly: drop policy, NO FORCE, DISABLE RLS,
drop FK + index + column. The backfill itself is not reversed (there
is nothing to reverse — dropping the column discards the values).

Revision ID: 0186_audit_snapshots_rls
Revises: 0185_audit_mode_vocab
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0186_audit_snapshots_rls"
down_revision: str | None = "0185_audit_mode_vocab"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "audit_snapshots"

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = (
    "(tenant_id IS NULL OR "
    "tenant_id = current_setting('app.current_tenant', true)::uuid)"
)


def upgrade() -> None:
    bind = op.get_bind()

    # ---- Step 1: add tenant_id NULLABLE. -------------------------------
    op.add_column(
        _TABLE,
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        f"{_TABLE}_tenant_id_fkey",
        _TABLE,
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])

    # ---- Step 2, pass 1: backfill from the row's own JSON. -------------
    # Any model that carries tenant_id as a real column had it serialised
    # into before_data/after_data by audit._row_to_dict — read it straight
    # back out, guarded by a UUID-shape regex so a malformed/unexpected
    # value can never abort the migration via a bad ::uuid cast.
    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE}
            SET tenant_id = (COALESCE(before_data->>'tenant_id', after_data->>'tenant_id'))::uuid
            WHERE tenant_id IS NULL
              AND COALESCE(before_data->>'tenant_id', after_data->>'tenant_id') ~ '{_UUID_RE}'
            """
        )
    )

    # ---- Step 2, pass 2: journal_lines fallback via sibling snapshot. --
    # journal_lines rows have no tenant_id column of their own; every one
    # was written alongside a journal_entries snapshot for the same
    # parent, in the same delete() transaction — self-join to that
    # sibling row (already resolved by pass 1) and copy its tenant_id.
    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE} AS line_snap
            SET tenant_id = entry_snap.tenant_id
            FROM {_TABLE} AS entry_snap
            WHERE line_snap.tenant_id IS NULL
              AND line_snap.table_name = 'journal_lines'
              AND entry_snap.table_name = 'journal_entries'
              AND entry_snap.tenant_id IS NOT NULL
              AND entry_snap.row_id = COALESCE(
                    line_snap.before_data->>'entry_id',
                    line_snap.after_data->>'entry_id'
                  )
            """
        )
    )

    # ---- Step 3: surface what's left NULL — never silently swallowed. --
    # Plain Python print (Alembic captures stdout to the migration log —
    # same precedent as 0101/0102) rather than a plpgsql RAISE NOTICE, to
    # avoid building a dynamic SQL string out of table_name values at all.
    remaining = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id IS NULL")
    ).scalar_one()
    if remaining:
        breakdown = bind.execute(
            sa.text(
                f"""
                SELECT table_name, count(*) AS n
                FROM {_TABLE}
                WHERE tenant_id IS NULL
                GROUP BY table_name
                ORDER BY n DESC
                """
            )
        ).all()
        detail = ", ".join(f"{row.table_name}={row.n}" for row in breakdown)
        print(
            f"0186: {remaining} audit_snapshots row(s) left with NULL "
            f"tenant_id after backfill (by table_name: {detail}) — these "
            f"are insertable but never SELECT-visible under RLS to any "
            f"tenant (fail-closed by design, see migration docstring). "
            f"Expected for table_name=settings (architecturally global, "
            f"no tenant to derive) — flag any OTHER table_name here for "
            f"review."
        )

    # ---- Step 4: ENABLE + FORCE RLS + ASYMMETRIC tenant_isolation. -----
    # See module docstring "Why the tenant_isolation policy is
    # ASYMMETRIC" for why WITH CHECK differs from USING here, unlike
    # every other tenant_isolation policy in this codebase.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.drop_constraint(f"{_TABLE}_tenant_id_fkey", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
