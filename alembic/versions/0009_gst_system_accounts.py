"""GST system accounts — auto-posting and settlement

Revision ID: 0009_gst_system_accounts
Revises: 0008_account_ranges
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_gst_system_accounts"
down_revision: str | None = "0008_account_ranges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add system_managed flag to accounts
    op.add_column(
        "accounts",
        sa.Column(
            "system_managed",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="System-managed accounts (GST, etc.) — auto-posted by the engine",
        ),
    )

    # Add GST settings
    for key, value in [
        ("gst_auto_post", "true"),
        ("gst_collected_account_code", '"21310"'),
        ("gst_paid_account_code", '"21330"'),
        ("gst_clearing_account_code", '"21320"'),
    ]:
        op.execute(
            f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}')"
            " ON CONFLICT (key) DO NOTHING"
        )

    # Mark existing GST accounts as system-managed
    op.execute(
        "UPDATE accounts SET system_managed = true "
        "WHERE code IN ('21310', '21330', '21320')"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'gst_auto_post', 'gst_collected_account_code', "
        "'gst_paid_account_code', 'gst_clearing_account_code')"
    )
    op.drop_column("accounts", "system_managed")
