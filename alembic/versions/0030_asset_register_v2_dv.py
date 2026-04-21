"""Asset register v2 — diminishing-value depreciation (Batch MM/1)

Adds the ``rate_pct`` column to ``depreciation_models`` and seeds six
AU-standard diminishing-value models (``asset_dv_40/30/20/15/10/5``).
The existing ``method`` CHECK is enforced at the Python layer
(``services.assets.cumulative_depreciation_through`` dispatches on
``method``); adding a new method here doesn't require a DB constraint
change.

DV rate values follow the AU TR 2023/1 capped-rate convention — users
don't enter a bare percentage, they pick a slug and we look up the
rate. The ``method_progress_factor`` column stays reserved for
non-linear DV variants (prime-cost x 2 rule, etc.) that ride on top
of the DV rate — we don't need it for v1.

Idempotent downgrade: drops the seeded rows and the column. Safe
because no ``fixed_assets.depreciation_model_id`` FK in production
will point at the seeded DV rows yet (this ships them for the first
time).

Revision ID: 0030_asset_register_v2_dv
Revises: 0029_user_preferred_theme
Create Date: 2026-04-21
"""
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision: str = "0030_asset_register_v2_dv"
down_revision: str | None = "0029_user_preferred_theme"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# AU-standard DV rates (prime-cost x 2 ≈ effective rate band).
# Rates are the per-year DV percentage applied to the opening book value.
_DV_SEEDS: list[tuple[str, str, int, int, str]] = [
    # (id,                    method,             number, period, rate_pct)
    ("asset_dv_40",           "diminishing_value", 0, 12, "40.0000"),
    ("asset_dv_30",           "diminishing_value", 0, 12, "30.0000"),
    ("asset_dv_20",           "diminishing_value", 0, 12, "20.0000"),
    ("asset_dv_15",           "diminishing_value", 0, 12, "15.0000"),
    ("asset_dv_10",           "diminishing_value", 0, 12, "10.0000"),
    ("asset_dv_5",            "diminishing_value", 0, 12, "5.0000"),
]


def upgrade() -> None:
    op.add_column(
        "depreciation_models",
        sa.Column(
            "rate_pct",
            sa.Numeric(7, 4),
            nullable=True,
            comment=(
                "Annual diminishing-value percentage (e.g. 30.0000 for "
                "30%). NULL for linear / no-depreciation models."
            ),
        ),
    )

    # Seed DV rows. ON CONFLICT DO NOTHING — re-running the migration
    # on a DB that already has these slugs is a no-op. Decimal typed
    # bind on ``rate`` so asyncpg sends it as numeric, not varchar.
    for slug, method, mnum, mper, rate in _DV_SEEDS:
        op.execute(
            sa.text(
                "INSERT INTO depreciation_models "
                "(id, method, method_number, method_period, rate_pct) "
                "VALUES (:id, :method, :mnum, :mper, :rate) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(
                sa.bindparam("id", slug, type_=sa.String(64)),
                sa.bindparam("method", method, type_=sa.String(32)),
                sa.bindparam("mnum", mnum, type_=sa.Integer()),
                sa.bindparam("mper", mper, type_=sa.Integer()),
                sa.bindparam(
                    "rate", Decimal(rate), type_=sa.Numeric(7, 4)
                ),
            )
        )


def downgrade() -> None:
    for slug, *_ in _DV_SEEDS:
        op.execute(
            sa.text(
                "DELETE FROM depreciation_models WHERE id = :id"
            ).bindparams(id=slug)
        )
    op.drop_column("depreciation_models", "rate_pct")
