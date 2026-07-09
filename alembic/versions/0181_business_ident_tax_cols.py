"""business_identifiers tax-identifier canonical gaps (M1.5 · T9).

Closes the delta between ``business_identifiers`` (0145, RLS'd in 0147)
and the fuller canonical tax-identifier shape the multi-jurisdiction
audit calls for: which jurisdiction an identifier belongs to, whether
its check digit validated, its validity window, and who issued it.

All five columns are additive and nullable — existing rows (every
scheme, every company) keep working unchanged. Only ``jurisdiction``
gets a value backfilled, and only for the one scheme that already has
a deterministic single-jurisdiction mapping: existing ``au_abn`` rows
are backfilled to ``'AUS'`` (matching the reference DB's jurisdiction
code, see ``saebooks/models/reference/jurisdiction.py`` and the AU
seed data). Other schemes are left NULL — the service layer derives a
jurisdiction on new writes (``services.business_identifiers``), but
retrofitting old rows for schemes this migration didn't introduce is
out of scope.

No RLS work needed: this is ``ADD COLUMN`` on an existing table, so
the 0145/0147 policy, FORCE RLS, coherence trigger, and grant are all
untouched.

See docs/multi-jurisdiction.md (M1.5) (theme T9).

Revision ID: 0181_business_ident_tax_cols
Revises: 0180_journal_line_tax_components
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0181_business_ident_tax_cols"
down_revision: str | None = "0180_journal_line_tax_components"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "business_identifiers"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("jurisdiction", sa.String(3), nullable=True))
    op.add_column(
        _TABLE, sa.Column("check_digit_valid", sa.Boolean(), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("valid_from", sa.Date(), nullable=True))
    op.add_column(_TABLE, sa.Column("valid_to", sa.Date(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("issuing_authority", sa.String(128), nullable=True)
    )

    # Backfill: every existing au_abn row is Australian by construction
    # (0145 backfilled it from companies.abn) — set jurisdiction='AUS'.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET jurisdiction = 'AUS' "
            "WHERE scheme = 'au_abn' AND jurisdiction IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "issuing_authority")
    op.drop_column(_TABLE, "valid_to")
    op.drop_column(_TABLE, "valid_from")
    op.drop_column(_TABLE, "check_digit_valid")
    op.drop_column(_TABLE, "jurisdiction")
