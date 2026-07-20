"""benefit_in_kind_rates — amount-per-unit columns (EE Packet 2, company-car
erisoodustus).

``benefit_in_kind_rates.rate_percent`` (0007) is percentage-shaped —
correct for AU FBT (47%) and EE's own generic ``general`` row (22.0000,
the income-tax leg's 22/78 numerator). It cannot represent EE's
company-car fringe benefit, whose statutory valuation is a EUR-per-kW-
per-month AMOUNT, not a percentage
(``saebooks/seeds/jurisdictions/EE/benefit_in_kind_rates.yaml``'s own
"COMPOUND-TAX SCHEMA LIMITATION" comment names this gap explicitly and
declines to force the amount into ``rate_percent``).

Two new NULLABLE columns give the amount-shaped case a real home without
touching ``rate_percent`` (kept NOT NULL — every row, including the new
car rows, still carries a ``rate_percent`` value for schema consistency
with the seeded ``general`` row; the car compute path
(``services.fringe_benefits_ee``) reads ``rate_amount_per_unit``
specifically and does not consult ``rate_percent`` for that case):

- ``rate_amount_per_unit`` — the per-unit amount (e.g. EUR 1.96/kW/month).
- ``rate_unit``            — free-text unit label (e.g. "eur_per_kw_per_month").

Both NULL for every pre-existing row (AU FBT, EE ``general``) — populated
only for the new EE ``motor_vehicle`` / ``motor_vehicle_aged`` rows this
packet's seed adds. Additive only, fully reversible via ``op.drop_column``.

Chains from the reference-DB single head ``0011_oss_member_state_rates``
(verified: no other file's ``down_revision`` names it).

Revision ID: 0012_bik_amount_per_unit
Revises:     0011_oss_member_state_rates
Create Date: 2026-07-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_bik_amount_per_unit"
down_revision: str | None = "0011_oss_member_state_rates"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "benefit_in_kind_rates"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "rate_amount_per_unit",
            sa.Numeric(9, 4),
            nullable=True,
            comment=(
                "Per-unit amount for an amount-shaped (not percentage-"
                "shaped) benefit valuation, e.g. 1.9600 (EUR/kW/month for "
                "the EE company-car benefit). NULL for every "
                "percentage-shaped row (AU FBT, EE general)."
            ),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "rate_unit",
            sa.String(32),
            nullable=True,
            comment=(
                "Free-text unit label for rate_amount_per_unit, e.g. "
                "'eur_per_kw_per_month'. NULL unless rate_amount_per_unit "
                "is set."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "rate_unit")
    op.drop_column(_TABLE, "rate_amount_per_unit")
