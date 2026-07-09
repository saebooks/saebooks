"""Drop the JournalEntry.tenant_id server-default.

When ``journal_entries.tenant_id`` was added in 0042, the column kept the
legacy dev tenant (``00000000-0000-0000-0000-000000000001``) as both
Python-side and DB-side default so the legacy ``services/journal.py`` —
which then didn't accept ``tenant_id`` as a parameter — could keep
working. The migration left a comment: "Phase 2 will drop the default
once all callers are migrated."

This is Phase 2. ``services/journal.py:create_draft`` now requires
``tenant_id`` (raises ``PostingError`` if ``None``) and ``reverse`` now
inherits ``tenant_id=original.tenant_id`` on the reversal JE. Every
caller in the codebase has been threaded through. With the default
removed, an INSERT that forgets ``tenant_id`` becomes an explicit DB
error instead of a silent cross-tenant write — defence in depth on
top of the Python guard. Required for tenant safety on any future
``JournalEntry()`` constructor invocation we don't already know about.

Revision ID: 0127_drop_journal_tenant_default
Revises: 0126_cashbook_default_bank_check
Create Date: 2026-05-24
"""
from __future__ import annotations

from alembic import op

revision: str = "0127_drop_journal_tenant_default"
down_revision: str | None = "0126_cashbook_default_bank_check"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE journal_entries ALTER COLUMN tenant_id DROP DEFAULT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE journal_entries ALTER COLUMN tenant_id "
        "SET DEFAULT '00000000-0000-0000-0000-000000000001'::uuid"
    )
