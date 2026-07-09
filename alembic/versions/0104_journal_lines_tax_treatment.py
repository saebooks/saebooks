"""journal_lines.tax_treatment JSONB snapshot column.

Adds a nullable JSONB column for the per-line tax-determination
snapshot produced by ``TaxEngine.compute``. Populated by the
post-from-invoice / post-from-bill paths going forward; pre-existing
rows stay null (no backfill — historic GST is already captured by the
existing ``gst_amount`` column and the BAS report tooling).

The column stores the ``TaxTreatment.to_jsonable()`` dict — Decimals
serialised as strings to preserve precision round-trip.

Revision ID: 0104_journal_lines_tax_treatment
Revises: 0102_alloc_invariants
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0104_journal_lines_tax_treatment"
down_revision: str | None = "0102_alloc_invariants"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "journal_lines",
        sa.Column("tax_treatment", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("journal_lines", "tax_treatment")
