"""Add trust_distributions and beneficiary_entitlements tables.

No distribution module;
year-end entitlements were untracked with no data model.

Revision ID: 0059_trust_distributions
Revises:     0058_user_tenant_memberships
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059_trust_distributions"
down_revision: str | None = "0058_user_tenant_memberships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trust_distributions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("financial_year", sa.Integer, nullable=False),
        sa.Column("distribution_date", sa.Date, nullable=False),
        sa.Column("resolution_minuted_date", sa.Date, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_trust_distributions_company_year",
        "trust_distributions",
        ["company_id", "financial_year"],
    )

    op.create_table(
        "beneficiary_entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "distribution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trust_distributions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("beneficiary_name", sa.String, nullable=False),
        sa.Column("percentage", sa.Numeric(7, 4), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notes", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_beneficiary_entitlements_distribution",
        "beneficiary_entitlements",
        ["distribution_id"],
    )


def downgrade() -> None:
    op.drop_table("beneficiary_entitlements")
    op.drop_table("trust_distributions")
