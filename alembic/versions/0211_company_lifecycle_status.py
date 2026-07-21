"""companies.lifecycle_status (M1.5 P1 tail).

Purely additive, following the 0198 pattern. Distinct from ``archived_at``
(saebooks soft-delete): this tracks the entity's real-world registration
lifecycle with its regulator (active / dormant / in_liquidation /
deregistered). Defaults "active" (server_default) — every existing company
keeps its current (implicit) status.

No RLS change — new column on an existing tenant-scoped table, not a new
table. Reversible: downgrade drops the column.

Revision ID: 0211_company_lifecycle_status
Revises: 0210_company_nfp_attributes
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0211_company_lifecycle_status"
down_revision: str | None = "0210_company_nfp_attributes"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "lifecycle_status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
    )


def downgrade() -> None:
    op.drop_column("companies", "lifecycle_status")
