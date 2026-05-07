"""Add bsl_matches junction table for N:1 bank-line reconciliation.

Gap P0-4 (small-dental-clinic 20260429T023124Z): the matched_to_* columns
on ``bank_statement_lines`` (added in 0052) only support 1:1 matching —
one BSL → one journal entry **or** one payment. Real bookkeeping needs
N:1: one bank deposit covers many invoice payments (e.g. a Medicare
batch EFT that pays 30 invoices in a single $5,000 transfer, a Stripe
payout consolidating a day's transactions).

This migration introduces a junction table and back-fills it from the
1:1 columns so existing reconciliation history is preserved. The
1:1 columns are kept (``matched_entry_id``, ``matched_to_type``,
``matched_to_id``, ``matched_at``, ``matched_by``) — readers and
external consumers may still rely on them. The application from this
revision onwards writes to the junction *and* keeps the 1:1 columns
populated with the most-recent / largest single match for backwards
compatibility. A future migration can drop them once every consumer
has moved across.

Schema
------
``bsl_matches``:
* ``id`` UUID PK
* ``bsl_id`` UUID FK → ``bank_statement_lines(id)`` ON DELETE CASCADE
* ``target_type`` VARCHAR(32) NOT NULL — ``PAYMENT`` | ``JOURNAL_ENTRY``
* ``target_id`` UUID NOT NULL — points at payments.id or journal_entries.id
* ``amount`` NUMERIC(14,2) NOT NULL — signed; sign must agree with the
  BSL.amount sign
* ``company_id`` UUID FK → ``companies(id)`` (CompanyScoped — RLS applies)
* ``tenant_id`` UUID FK → ``tenants(id)`` ON DELETE RESTRICT
* ``notes`` TEXT — optional human note for partial allocations
* ``matched_by`` VARCHAR — user/admin marker, mirrors BSL.matched_by
* ``created_at`` TIMESTAMPTZ NOT NULL DEFAULT now()
* ``archived_at`` TIMESTAMPTZ — soft-delete on un-match

Indexes
-------
* ``ix_bsl_matches_bsl`` on ``(bsl_id)`` — primary read pattern is "all
  matches for this BSL", which the recompute_status helper hits on
  every add/remove.
* ``ix_bsl_matches_target`` on ``(target_type, target_id)`` — lets the
  payments/JE detail pages show "this transaction reconciles against
  these BSLs".
* ``ix_bsl_matches_company`` on ``(company_id, archived_at)`` — supports
  the dashboard "unreconciled count" widget.

Note: NO unique constraint on (bsl_id, target_type, target_id) because
splitting the same payment across two BSLs (e.g. partial deposit that
clears in two settlements) is legitimate, and the junction is the
right place to model that without a sub-line workaround.

Revision ID: 0082_bsl_matches
Revises: 0081_oauth_and_fido2
Create Date: 2026-05-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0082_bsl_matches"
down_revision: str | None = "0081_oauth_and_fido2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "bsl_matches"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "bsl_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bank_statement_lines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(
                "'00000000-0000-0000-0000-000000000001'::uuid"
            ),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("matched_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('PAYMENT', 'JOURNAL_ENTRY')",
            name="bsl_matches_target_type_check",
        ),
    )
    op.create_index(
        "ix_bsl_matches_bsl", _TABLE, ["bsl_id"]
    )
    op.create_index(
        "ix_bsl_matches_target", _TABLE, ["target_type", "target_id"]
    )
    op.create_index(
        "ix_bsl_matches_company",
        _TABLE,
        ["company_id", "archived_at"],
    )

    # ------------------------------------------------------------------ #
    # Backfill from existing matched_to_* / matched_entry_id columns.    #
    #                                                                     #
    # Two source columns:                                                #
    #   * matched_to_type / matched_to_id (added in 0052) — preferred    #
    #     because it carries the type tag.                                #
    #   * matched_entry_id — older, JE-only, only used when matched_to_* #
    #     is NULL.                                                       #
    #                                                                     #
    # Amount: the existing 1:1 model implies amount == BSL.amount, so we #
    # carry that across verbatim. Sign already matches by definition.    #
    # ------------------------------------------------------------------ #
    op.execute(
        sa.text(
            """
            INSERT INTO bsl_matches
                (id, bsl_id, target_type, target_id, amount,
                 company_id, tenant_id, matched_by, created_at)
            SELECT
                gen_random_uuid(),
                bsl.id,
                bsl.matched_to_type,
                bsl.matched_to_id,
                bsl.amount,
                bsl.company_id,
                bsl.tenant_id,
                bsl.matched_by,
                COALESCE(bsl.matched_at, bsl.created_at)
            FROM bank_statement_lines bsl
            WHERE bsl.matched_to_type IS NOT NULL
              AND bsl.matched_to_id IS NOT NULL
              AND bsl.archived_at IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO bsl_matches
                (id, bsl_id, target_type, target_id, amount,
                 company_id, tenant_id, matched_by, created_at)
            SELECT
                gen_random_uuid(),
                bsl.id,
                'JOURNAL_ENTRY',
                bsl.matched_entry_id,
                bsl.amount,
                bsl.company_id,
                bsl.tenant_id,
                bsl.matched_by,
                COALESCE(bsl.matched_at, bsl.created_at)
            FROM bank_statement_lines bsl
            WHERE bsl.matched_entry_id IS NOT NULL
              AND bsl.matched_to_type IS NULL
              AND bsl.matched_to_id IS NULL
              AND bsl.archived_at IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_bsl_matches_company", table_name=_TABLE)
    op.drop_index("ix_bsl_matches_target", table_name=_TABLE)
    op.drop_index("ix_bsl_matches_bsl", table_name=_TABLE)
    op.drop_table(_TABLE)
