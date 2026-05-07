"""Multi-tenant foundation — create tenants table with one default tenant.

Revision ID: 0040_tenants
Revises: 0039_phase1_item_version
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_tenants"
down_revision: str | None = "0039_phase1_item_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_tenants_slug", "tenants", ["slug"])

    # Seed the default tenant with a well-known UUID so migration 0041
    # can reference it by value.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, slug) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'Default', 'default')"
        )
    )


def downgrade() -> None:
    op.drop_table("tenants")
