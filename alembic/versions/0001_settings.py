"""settings table

Revision ID: 0001_settings
Revises:
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_settings"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
    )

    import json

    defaults: list[tuple[str, object]] = [
        ("fin_year_start_month", 7),
        ("base_currency", "AUD"),
        ("gst_rounding_sales", "DOWN"),
        ("gst_rounding_purchases", "UP"),
        ("gst_calc_level", "LINE"),
        ("audit_mode", "immutable"),
        ("retention_years_journal", 7),
        ("retention_years_attachments", 7),
    ]
    for k, v in defaults:
        op.execute(
            sa.text(
                "INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
            ).bindparams(k=k, v=json.dumps(v))
        )


def downgrade() -> None:
    op.drop_table("settings")
