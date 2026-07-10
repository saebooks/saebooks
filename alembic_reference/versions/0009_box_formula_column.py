"""tax_return_box_definitions.formula column (M1.5 · KMD-formula Packet 1).

Additive, nullable ``formula`` column on ``tax_return_box_definitions`` for
the new ``formula`` aggregation kind (box arithmetic: box references,
``+ - *``, and ``max(0, <expr>)`` — see
``saebooks.services.tax_return_generator``'s formula-parser docstring).

Dedicated column, not an inline ``formula:<expr>`` suffix on the existing
``aggregation`` column: ``aggregation`` is ``String(64)`` and KMD box 4's
rate-formula (``0.24*KMD:1 + 0.20*KMD:1-1 + 0.22*KMD:1-2 + 0.09*KMD:2 + ...``)
is ~90 chars, so it does not fit. ``aggregation = "formula"`` is now the
short discriminator; the expression lives here.

Nullable ⇒ no ``server_default`` needed (only required for a NOT NULL
column added to an existing table with rows). Fully reversible: a bare
``DROP COLUMN``, no data backfill, no other table touched.

See ~/.claude/plans/kmd-formula-support-scope.md §3.1/§4 (KMD-formula
support scope, Packet 1).

Revision ID: 0009_box_formula_column
Revises: 0008_retire_vehicle_contrib
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_box_formula_column"
down_revision: str | None = "0008_retire_vehicle_contrib"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_return_box_definitions",
        sa.Column("formula", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tax_return_box_definitions", "formula")
