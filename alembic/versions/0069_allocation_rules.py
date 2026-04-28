"""Add allocation_rules table for overhead allocation engine.

Gap FITC-6 (medium-fitness-chain): /allocations and /settings/allocations
returned 404 because no allocation rules engine existed. This migration
adds the persistence layer.

Revision ID: 0069_allocation_rules
Revises: 0068_department_cost_centre
Create Date: 2026-04-28
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0069_allocation_rules"
down_revision: str | None = "0068_department_cost_centre"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "allocation_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "source_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("targets", JSONB, nullable=False, server_default="'[]'::jsonb"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_allocation_rules_company_id",
        "allocation_rules",
        ["company_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_allocation_rules_company_id", "allocation_rules")
    op.drop_table("allocation_rules")
