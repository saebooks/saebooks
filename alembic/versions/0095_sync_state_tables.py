"""Stub for 0095_sync_state_tables.

This revision was applied to some environments before 0095_quotes_tables was
branched off 0094_purchase_orders. The stub makes the alembic revision map
consistent across all environments.

Revision ID: 0095_sync_state_tables
Revises: 0094_purchase_orders
Create Date: 2026-05-09
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0095_sync_state_tables"
down_revision: str | None = "0094_purchase_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
