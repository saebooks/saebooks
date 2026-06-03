"""Add WRITTEN_OFF invoice status + write_off_journal_entry_id column.

Commit 4800930 (branch feat/invoice-written-off) added the WRITTEN_OFF enum
value and the invoices.write_off_journal_entry_id FK to the ORM model but
shipped no migration, leaving the model ahead of the schema — every rebuild
from this branch 500'd with "column invoices.write_off_journal_entry_id does
not exist". This migration makes the database match the model.

Mirrors the existing journal_entry_id / void_journal_entry_id columns on the
same table, and follows the enum-value pattern from 0060.

Revision ID: 0142_invoice_written_off
Revises:     0141_super_lodgements
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0142_invoice_written_off"
down_revision: str | None = "0141_super_lodgements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New terminal status for invoices settled by write-off. PG16 permits
    # ADD VALUE inside a transaction provided the value is not used in the
    # same migration (it isn't here).
    op.execute("ALTER TYPE invoice_status_enum ADD VALUE IF NOT EXISTS 'WRITTEN_OFF'")

    # Journal entry that booked the write-off; nullable, SET NULL on delete to
    # mirror journal_entry_id / void_journal_entry_id on invoices.
    op.add_column(
        "invoices",
        sa.Column(
            "write_off_journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("invoices", "write_off_journal_entry_id")
    # NOTE: PostgreSQL cannot drop an enum value; 'WRITTEN_OFF' is intentionally
    # left on invoice_status_enum (harmless when unused).
