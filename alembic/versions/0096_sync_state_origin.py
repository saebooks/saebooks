"""sync_state.origin — explicit tri-state for push-eligibility.

Background
----------
Migration 0095 added ``sync_state`` with ``last_pushed_version`` and the
push selector ``last_pushed_version IS NULL OR version > last_pushed_version``.
That selector overloads NULL with two meanings:

1. **never pushed** — i.e. local-only row, needs first push
2. **just pulled** — i.e. arrived from upstream, must NOT be pushed back

Commit ``e5a092c`` papered over the bug by having the pull path stamp
``last_pushed_version=existing.version`` on upsert so pulled rows look
"already pushed" to the selector. That works for Xero but is fragile:
the schema is lying to its own consumers, and any future MYOB/QBO
adapter writer who follows the obvious path of "leave
last_pushed_version NULL on insert" reintroduces the bug.

This migration replaces the workaround with explicit tri-state
semantics so the schema cannot be misread.

The ``origin`` column
---------------------
``origin`` is ``TEXT NOT NULL`` with a CHECK constraint to one of:

* ``'local'``   — row was created locally; needs first push
* ``'remote'``  — row was pulled from upstream; do NOT push unless
                  the local copy has been edited since pull
                  (``contact.version > 1`` / ``invoice.version > 1``)
* ``'synced'``  — has been successfully pushed at least once;
                  push-eligible iff ``version > last_pushed_version``

The push selector becomes:

* ``origin = 'local'``  AND ``last_pushed_version IS NULL``        → first push
* ``origin = 'synced'`` AND ``version > last_pushed_version``      → update push
* ``origin = 'remote'`` AND ``version > 1``                        → push remote-edited-locally;
                                                                     transitions to 'synced' on success

Backfill
--------
For rows already in ``sync_state`` at the time of this migration:

* ``last_pushed_version IS NULL``   → ``'remote'``
  Justification: pre-fix workaround stamps ``last_pushed_version=version``
  on pull, so any row in the wild with a NULL ``last_pushed_version`` is
  either a genuine never-pushed local row OR a pre-fix pulled row. We
  call it ``'remote'`` to be safe — a false-negative on re-push (operator
  has to nudge the row) is preferable to a false-positive (overwriting
  the upstream link with a fresh push response).

* ``last_pushed_version IS NOT NULL`` → ``'synced'``
  These rows have demonstrably been pushed (or stamped by the workaround
  on pull). Either way ``synced`` is the correct steady-state semantic;
  the next push sweep will find them only if their ``version`` advances.

Index
-----
Push-selector queries filter on ``(connection_id, object_type, origin,
quarantined)`` — a partial index on ``(connection_id, object_type)``
``WHERE quarantined = false`` already exists implicitly via the
unique-on-external_id index, but a covering composite index lets the
push selector skip the join's filter cost. The dataset is per-tenant
small (low thousands), but the index is cheap and future-proofs MYOB.

Reversibility
-------------
``downgrade()`` drops the index, the constraint, then the column.
Existing rows are unaffected — the workaround pull-path version
stamping (commit ``e5a092c``) still works without ``origin`` because
the old NULL-overload selector still parses, just with the same bug.

Revision ID: 0096_sync_state_origin
Revises: 0095_sync_state_tables
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0096_sync_state_origin"
down_revision: str | None = "0095_sync_state_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add nullable first so the backfill can run.
    op.add_column(
        "sync_state",
        sa.Column(
            "origin",
            sa.Text(),
            nullable=True,
            comment="'local' | 'remote' | 'synced' — see migration "
            "0096 docstring. Replaces the NULL-overloading of "
            "last_pushed_version. CHECK constraint on this column.",
        ),
    )

    # 2. Backfill — see docstring for the rationale on the NULL bucket.
    op.execute(
        sa.text(
            "UPDATE sync_state "
            "SET origin = CASE "
            "  WHEN last_pushed_version IS NULL THEN 'remote' "
            "  ELSE 'synced' "
            "END "
            "WHERE origin IS NULL"
        )
    )

    # 3. Lock down: NOT NULL + CHECK constraint.
    op.alter_column("sync_state", "origin", nullable=False)
    op.create_check_constraint(
        "ck_sync_state_origin",
        "sync_state",
        "origin IN ('local', 'remote', 'synced')",
    )

    # 4. Push-selector covering index.
    op.create_index(
        "ix_sync_state_push_selector",
        "sync_state",
        ["connection_id", "object_type", "origin"],
        postgresql_where=sa.text("quarantined = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_sync_state_push_selector", table_name="sync_state")
    op.drop_constraint("ck_sync_state_origin", "sync_state", type_="check")
    op.drop_column("sync_state", "origin")
