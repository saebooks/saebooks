"""Add external_id columns + partial unique index to sub-ledger tables.

Sub-ledger tables (``bills``, ``invoices``, ``credit_notes``,
``payments``) need a stable identifier for the upstream system that
imported them — so a re-run of the QBO migration script (or any future
sync) updates the same row instead of inserting a duplicate.

Why these four tables only
--------------------------
They are the source-of-truth sub-ledger entries that must round-trip
to / from external systems. ``journal_entries`` is the *result* of a
sub-ledger event, not the event itself, so it does not get
external_id columns — the lookup goes from the upstream id → bill /
invoice / etc. → ``journal_entry_id`` (already on the row).

Why JSONB for the payload
-------------------------
Audit / forensic. We keep a verbatim copy of the QBO API response (or
whatever upstream gave us) so the importer can be re-run with new
mapping rules without re-fetching from QBO. Strict JSONB rather than
TEXT so dotted-path queries work in psql.

Why partial unique index
------------------------
Pre-existing rows (from QBO Phase A / placeholder data) may have NULL
external_id and there can legitimately be more than one. The unique
constraint applies only when the row claims an external identity —
``WHERE external_id IS NOT NULL``.

Class-A RLS — no changes needed
-------------------------------
All four tables already have tenant_id + ENABLE / FORCE RLS +
``tenant_isolation`` policy from migration 0041 (and 0055 for FORCE).
Adding columns to an RLS-enabled table does NOT require any policy or
ALTER TABLE work — Postgres applies the existing predicate to the
wider row.

Reversibility
-------------
``downgrade()`` drops the index then the columns, in reverse order.

Revision ID: 0092_external_ids_subledger
Revises: 0091_wizard_state_force_rls
Create Date: 2026-05-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0092_external_ids_subledger"
down_revision: str | None = "0091_wizard_state_force_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("bills", "invoices", "credit_notes", "payments")


def _columns() -> list[sa.Column]:
    """The four sibling columns added to every sub-ledger table.

    Built fresh per ``add_column`` call because alembic mutates the
    Column object during emit.
    """
    return [
        sa.Column(
            "external_id",
            sa.String(255),
            nullable=True,
            comment=(
                "Upstream identifier (e.g. QBO Bill.Id, Xero "
                "InvoiceID) used to dedupe on resync."
            ),
        ),
        sa.Column(
            "external_source",
            sa.String(64),
            nullable=True,
            comment=(
                "Source system that issued external_id — 'qbo', "
                "'xero', 'myob', 'csv-2026-04', etc."
            ),
        ),
        sa.Column(
            "external_etag",
            sa.String(255),
            nullable=True,
            comment=(
                "Upstream optimistic-concurrency token (QBO SyncToken,"
                " Xero UpdatedDateUTC). Sync engine uses this to "
                "detect upstream changes between resyncs."
            ),
        ),
        sa.Column(
            "external_payload",
            postgresql.JSONB(),
            nullable=True,
            comment=(
                "Verbatim upstream payload at last fetch — kept for "
                "audit / forensic / re-mapping without re-fetching."
            ),
        ),
    ]


def upgrade() -> None:
    for table in _TABLES:
        for col in _columns():
            op.add_column(table, col)
        op.create_index(
            f"uq_{table}_external_id",
            table,
            ["company_id", "external_source", "external_id"],
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"uq_{table}_external_id", table_name=table)
        op.drop_column(table, "external_payload")
        op.drop_column(table, "external_etag")
        op.drop_column(table, "external_source")
        op.drop_column(table, "external_id")
