"""Phase 1 tier-4 — add matched_to_type and matched_to_id to bank_statement_lines.

These two columns extend the existing reconciliation fields (matched_entry_id,
matched_at, matched_by) to support matching against both journal entries and
payments via the new /match and /unmatch action endpoints.

- matched_to_type: VARCHAR(32), nullable — 'PAYMENT' or 'JOURNAL_ENTRY'
- matched_to_id:   UUID, nullable — UUID of the matched record (no FK constraint,
                   as it may point to either payments or journal_entries)

The existing ``status`` column (UNMATCHED/MATCHED/IGNORED) continues to serve
as the reconciliation status flag.

Revision ID: 0052_bsl_reconciliation
Revises: 0051_budgets_tv
Create Date: 2026-04-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0052_bsl_reconciliation"
down_revision: str | None = "0051_budgets_tv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "bank_statement_lines"


def _col_exists(table: str, col: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": col},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # bank_statement_lines — matched_to_type                              #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "matched_to_type"):
        op.add_column(
            _TABLE,
            sa.Column("matched_to_type", sa.String(32), nullable=True),
        )

    # ------------------------------------------------------------------ #
    # bank_statement_lines — matched_to_id                                #
    # ------------------------------------------------------------------ #
    if not _col_exists(_TABLE, "matched_to_id"):
        op.add_column(
            _TABLE,
            sa.Column("matched_to_id", PG_UUID(as_uuid=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column(_TABLE, "matched_to_id")
    op.drop_column(_TABLE, "matched_to_type")
