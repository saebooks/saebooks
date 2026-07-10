"""social_contribution_schemes.wage_base_floor (kmd-inf-tsd scope Packet 3).

Closes the gap ``social_contribution_schemes.yaml``'s own header comment
flags: EE social tax (sotsiaalmaks) has a statutory MONTHLY MINIMUM BASE
(EUR 886 for 2026 — giving a minimum monthly employer liability of
EUR 292.38 = 886 x 33%) that applies regardless of actual wages paid.
The table only had ``wage_base_cap`` (an upper bound the rate stops
applying above); there was no floor/minimum-base field, so the EE
minimum-base rule could not be faithfully represented. This adds the
mirror-image column.

Additive, nullable, no backfill, no server_default — every existing
scheme row (AU Medicare levy, EE unemployment, any future jurisdiction)
is unaffected; only the reseeded ``ee_social_tax`` row gets a value.
Fully reversible via ``op.drop_column``.

Revision ID: 0010_social_tax_floor
Revises:     0009_box_formula_column
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_social_tax_floor"
down_revision: str | None = "0009_box_formula_column"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "social_contribution_schemes"
_COLUMN = "wage_base_floor"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Numeric(14, 2),
            nullable=True,
            comment=(
                "Minimum monthly wage base the contribution rate is "
                "assessed against, regardless of actual wages paid "
                "(e.g. EE sotsiaalmaks EUR 886/mo). NULL = no floor "
                "(the pre-existing default for every other scheme)."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
