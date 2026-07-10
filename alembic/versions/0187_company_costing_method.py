"""company.costing_method — per-company inventory costing policy (Wave D)

Revision ID: 0185_company_costing_method
Revises:     0184_jltc_grant_app_role
Create Date: 2026-07-10

Richard's decision (2): inventory costing is a PER-COMPANY SETTING, not a
forced method. This adds a single scalar policy column on ``companies``
alongside the other per-company switches (``bookkeeping_mode`` /
``audit_mode`` / ``writeoff_mode``):

* ``costing_method`` — ``weighted_average`` | ``fifo`` | ``quantity_only``.

Additive + backward-safe:

* ``server_default='weighted_average'`` so every EXISTING company row is
  backfilled to the pre-Wave-D behaviour (the inventory module was
  WAC-only / WAC-locked before this wave). No existing WAC company is
  affected.
* A CHECK constraint restricts the value set at the DB layer; the Python
  ``CostingMethod`` StrEnum is the first line of defence.

Reversible: ``downgrade`` drops the constraint + column.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0187_company_costing_method"
down_revision: str | None = "0186_audit_snapshots_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METHODS = ("weighted_average", "fifo", "quantity_only")


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "costing_method",
            sa.String(24),
            nullable=False,
            server_default="weighted_average",
        ),
    )
    op.create_check_constraint(
        "ck_companies_costing_method_valid",
        "companies",
        "costing_method IN ('" + "', '".join(_METHODS) + "')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_companies_costing_method_valid", "companies", type_="check"
    )
    op.drop_column("companies", "costing_method")
