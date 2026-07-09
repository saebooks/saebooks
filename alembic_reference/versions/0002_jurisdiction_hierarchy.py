"""jurisdictions: multi-level hierarchy (M1.5 · T3).

Adds three additive columns to the reference ``jurisdictions`` table so a
jurisdiction can be a node in a tree — a country that owns sub-national
tax jurisdictions (state / province / county / city). This is the
foundation that lets the engine represent US federal + state + local
sales tax, Canada federal GST + provincial PST/HST, sub-national VAT,
and state-levied stamp duty. See
``docs/multi-jurisdiction.md`` (M1.5) (theme T3).

Purely additive and non-breaking:
  - ``parent_code``  — nullable self-FK to jurisdictions.code (top-level
    countries have no parent).
  - ``level``        — NOT NULL, server_default 'country'; existing rows
    backfill to country-level.
  - ``iso_subdivision_code`` — nullable ISO 3166-2 subdivision code.

The 15 existing reference tables that FK to ``jurisdictions.code`` are
unaffected — a state-level node is just another row with the same PK
shape.

Revision ID: 0002_jurisdiction_hierarchy
Revises: 0001_initial_reference_schema
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_jurisdiction_hierarchy"
down_revision: str | None = "0001_initial_reference_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jurisdictions",
        sa.Column(
            "parent_code",
            sa.String(3),
            sa.ForeignKey("jurisdictions.code"),
            nullable=True,
        ),
    )
    op.add_column(
        "jurisdictions",
        sa.Column(
            "level",
            sa.String(16),
            nullable=False,
            server_default="country",
        ),
    )
    op.add_column(
        "jurisdictions",
        sa.Column("iso_subdivision_code", sa.String(6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jurisdictions", "iso_subdivision_code")
    op.drop_column("jurisdictions", "level")
    op.drop_column("jurisdictions", "parent_code")
