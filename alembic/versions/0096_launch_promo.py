"""0096_launch_promo — launch-promo JWT stamp on users table.

Adds ``users.launch_promo_jwt`` (nullable TEXT) to cache the
Ed25519-signed Pro-tier JWT issued by the license-server during the
first-1000-customers promo. NULL = no promo. The JWT is verified at
login by the licence resolver — if it validates and is not expired the
session runs at Pro tier without contacting the license-server.

Revision ID: 0096_launch_promo
Revises: 0095_sync_state_tables
Create Date: 2026-05-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0096_launch_promo"
down_revision: str | None = "0095_sync_state_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "launch_promo_jwt",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "launch_promo_jwt")
