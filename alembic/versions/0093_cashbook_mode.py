"""Cashbook edition — `bookkeeping_mode`, default bank, category overrides.

Adds three columns to ``companies`` so a company can be flipped into
single-entry "cashbook" UX while the underlying ledger stays
double-entry. See ``docs/cashbook-edition-design.md`` for the full
design rationale.

Why on ``companies`` and not a new table
----------------------------------------
Cashbook is a UI mode, not a parallel ledger. Every cashbook entry
compiles to a real ``journal_entries`` + 2-3 ``journal_lines`` rows.
The only per-company state needed is (a) which mode the company is in,
(b) which bank account is the implicit counter-account in cashbook
mode, (c) optional per-company category overrides. Three columns is
the right shape; a side table would be premature.

Why CHECK constraint
--------------------
Fail closed. A company in ``bookkeeping_mode='cashbook'`` without a
default bank account is unusable — every cashbook entry needs a bank
account to debit/credit. The CHECK refuses the bad state at the DB
layer so a partial migration or buggy admin tool can't leave a
customer staring at an "unconfigured" error every time they tap +.

Why JSONB for ``cashbook_categories``
-------------------------------------
~20 default categories live in code (``services/cashbook_categories``).
Average sole trader will override zero or one ("rename Vehicle to Ute &
fuel"). A side table for ~30 default rows × N companies, almost all
empty, is wasteful. JSONB lets the resolver merge defaults with sparse
overrides at read time and lets us version the default list in code
without a per-customer migration. Revisit when avg override count > 5.

Class-A RLS — no changes needed
-------------------------------
``companies`` already has tenant_id + ENABLE / FORCE RLS +
``tenant_isolation`` policy. Adding columns to an RLS-enabled table
doesn't require any policy work — Postgres applies the existing
predicate to the wider row.

Reversibility
-------------
``downgrade()`` drops the CHECK constraint then the columns in reverse
order. Existing data is not preserved on downgrade — this is a
schema-only migration; if a company has been flipped to cashbook the
mode is lost on downgrade (which is fine: downgrade returns the
schema to pre-cashbook, so cashbook mode is meaningless).

Revision ID: 0093_cashbook_mode
Revises: 0092_external_ids_subledger
Create Date: 2026-05-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0093_cashbook_mode"
down_revision: str | None = "0092_external_ids_subledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "bookkeeping_mode",
            sa.String(16),
            server_default="full",
            nullable=False,
            comment=(
                "UX mode: 'full' (default — double-entry surface) or "
                "'cashbook' (single-entry UX over the same ledger). "
                "Drives menu visibility and which routers are exposed."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "cashbook_default_bank_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=True,
            comment=(
                "Implicit counter-account for cashbook entries. Required "
                "iff bookkeeping_mode='cashbook' (enforced by CHECK)."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "cashbook_categories",
            postgresql.JSONB(),
            nullable=True,
            comment=(
                "Per-company overrides on the default cashbook category "
                "list. Shape: {version, overrides: {CODE: {label?, "
                "account_id?, hidden?}}}. NULL = bare defaults."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_cashbook_requires_bank",
        "companies",
        "bookkeeping_mode <> 'cashbook' "
        "OR cashbook_default_bank_account_id IS NOT NULL",
    )

    # Service-level idempotency for record_cashbook_entry. The cashbook
    # service stamps ``attachments->'cashbook_meta'->>'idempotency_key'``
    # on every JE it creates; the partial unique index makes "same key
    # twice for the same company" a DB-level conflict the service can
    # catch and replay. Scoped per company_id (different companies
    # using the same client-generated key don't collide).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_je_cashbook_idempotency
        ON journal_entries (
            company_id,
            (attachments #>> '{cashbook_meta,idempotency_key}')
        )
        WHERE attachments #>> '{cashbook_meta,idempotency_key}' IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_je_cashbook_idempotency")
    op.drop_constraint("ck_cashbook_requires_bank", "companies", type_="check")
    op.drop_column("companies", "cashbook_categories")
    op.drop_column("companies", "cashbook_default_bank_account_id")
    op.drop_column("companies", "bookkeeping_mode")
