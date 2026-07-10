"""Add account_kind column to accounts and backfill bank-side rows.

Background
----------
Before this migration, ``Account`` rows had no ``account_kind`` field —
the column was advertised by ``BankAccountCreate`` / ``BankAccountOut``
on the API but never persisted, so every account came back with
``account_kind=None`` and the web layer's ``_KIND_SECTIONS`` bucketing
was dead code.  As a consequence, the "Bank Accounts" page filtered by
``bsb IS NOT NULL`` (the only physical signal of bank-side-ness),
which silently excluded credit cards, loans, and cash accounts.

Idempotency
-----------
On several stacks (primary, acme, app-preview, cashbook-demo) the
``account_kind`` column was hand-rolled before this migration was
authored — as an enum (``account_kind_enum``) with rows already
classified.  This migration therefore creates the type + column only
if they don't already exist, and only backfills BSB-bearing rows
whose kind is still NULL.

Reversibility
-------------
``downgrade()`` drops the column and the type.  Existing classification
is lost.

Revision ID: 0119_account_kind
Revises: 0118_change_log_tenant_id
Create Date: 2026-05-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0119_account_kind"
down_revision: str | None = "0118_change_log_tenant_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_VALUES = ("BANK_CHECKING", "BANK_SAVINGS", "CREDIT_CARD", "BANK_LOAN", "CASH", "OTHER")


def upgrade() -> None:
    bind = op.get_bind()

    # Create enum type only if missing.
    type_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'account_kind_enum'")
    ).fetchone()
    if not type_exists:
        op.execute(
            sa.text(
                "CREATE TYPE account_kind_enum AS ENUM "
                "('BANK_CHECKING','BANK_SAVINGS','CREDIT_CARD','BANK_LOAN','CASH','OTHER')"
            )
        )

    # Add column only if missing.
    column_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'accounts' "
            "AND column_name = 'account_kind'"
        )
    ).fetchone()
    if not column_exists:
        op.execute(
            sa.text(
                "ALTER TABLE accounts ADD COLUMN account_kind account_kind_enum NULL"
            )
        )

    # Backfill BSB-bearing rows that haven't been classified yet.
    op.execute(
        sa.text(
            "UPDATE accounts SET account_kind = 'BANK_CHECKING' "
            "WHERE bsb IS NOT NULL AND account_kind IS NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE accounts DROP COLUMN IF EXISTS account_kind"))
    op.execute(sa.text("DROP TYPE IF EXISTS account_kind_enum"))
