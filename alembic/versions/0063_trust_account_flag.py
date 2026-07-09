"""Add is_trust_account flag to accounts table.

Vendor deposits were movable from trust to
operating in a single JE with no validation. This boolean enables the journal
posting service to detect and block such commingling.

Revision ID: 0063_trust_account_flag
Revises: 0062_deferred_revenue
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0063_trust_account_flag"
down_revision: str | None = "0062_deferred_revenue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "is_trust_account",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "is_trust_account")
