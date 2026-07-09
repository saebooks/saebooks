"""0153_je_provenance — JE-provenance keystone columns on journal_entries.

Adds three columns recording what created each journal entry:

  - ``origin``      String(32) NOT NULL DEFAULT 'UNKNOWN' — a JournalOrigin
                    StrEnum value (MANUAL / INVOICE / BILL / ...). Stored as a
                    plain string (no DB enum type) to mirror the existing
                    ``status``/``EntryStatus`` pattern.
  - ``source_type`` String(64) NULL — the originating record's type string
                    (e.g. "invoice", "bill", "payment") where the origin has
                    one; NULL for MANUAL / UNKNOWN / period-close style origins.
  - ``source_id``   UUID NULL — the originating record's id.

Forward-only: the NOT NULL default 'UNKNOWN' keeps every existing row valid
WITHOUT any historical backfill. Provenance is a claim about NEW posts only —
the posting chokepoint (``services.journal.post`` / ``post_in_txn``) stamps the
real origin (default MANUAL) on entries posted from here on. We deliberately do
NOT scan invoice.journal_entry_id / bill.journal_entry_id / etc. to retro-stamp
old rows; that inverse back-ref stays the source of truth for historical
provenance and any guessed origin would be unverifiable.

No CHECK constraint on ``origin`` — same as ``status`` (a plain String(16) with
no DB-level value check); the StrEnum is enforced in Python at the chokepoint.

Reversible: downgrade drops the three columns.

Revision ID: 0153_je_provenance
Revises:     0152_journal_company_structural
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0153_je_provenance"
down_revision: str | None = "0152_journal_company_structural"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # origin — NOT NULL with server_default 'UNKNOWN' so existing rows are
    # valid immediately (forward-only, no backfill). New posts overwrite the
    # default with their real origin at the posting chokepoint.
    op.add_column(
        "journal_entries",
        sa.Column(
            "origin",
            sa.String(length=32),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    # source_type — nullable; the originating record's type string.
    op.add_column(
        "journal_entries",
        sa.Column("source_type", sa.String(length=64), nullable=True),
    )
    # source_id — nullable; the originating record's id.
    op.add_column(
        "journal_entries",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    # Index the (source_type, source_id) pair so reverse lookups
    # ("which JE did invoice X create?") and provenance audits are cheap.
    op.create_index(
        "ix_journal_entries_source",
        "journal_entries",
        ["source_type", "source_id"],
    )
    # Index origin for "show me all MANUAL entries" — the visible-exception
    # query this keystone exists to make fast.
    op.create_index(
        "ix_journal_entries_origin",
        "journal_entries",
        ["origin"],
    )


def downgrade() -> None:
    op.drop_index("ix_journal_entries_origin", table_name="journal_entries")
    op.drop_index("ix_journal_entries_source", table_name="journal_entries")
    op.drop_column("journal_entries", "source_id")
    op.drop_column("journal_entries", "source_type")
    op.drop_column("journal_entries", "origin")
