"""Add BENEFICIARY contact type and beneficiary-specific columns.

Reason: /beneficiaries 404, no
BENEFICIARY contact type, no TFN or share-percentage fields.

Changes:
- Add BENEFICIARY value to contact_type_enum
- Add tfn, share_percentage, default_income_classification to contacts
- Add optional contact_id FK to beneficiary_entitlements

Revision ID: 0060_beneficiary_contact_type
Revises:     0059_trust_distributions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060_beneficiary_contact_type"
down_revision: str | None = "0059_trust_distributions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new enum value — cannot use IF NOT EXISTS in older PG but we're on 16.
    op.execute("ALTER TYPE contact_type_enum ADD VALUE IF NOT EXISTS 'BENEFICIARY'")

    # Beneficiary-specific columns on contacts (all nullable — only populated
    # when contact_type = BENEFICIARY).
    op.add_column(
        "contacts",
        sa.Column("tfn", sa.String(11), nullable=True,
                  comment="Tax File Number — 8 or 9 digits, stored without spaces"),
    )
    op.add_column(
        "contacts",
        sa.Column("share_percentage", sa.Numeric(7, 4), nullable=True,
                  comment="Default entitlement share (0.0000 – 100.0000)"),
    )
    op.add_column(
        "contacts",
        sa.Column("default_income_classification", sa.String(64), nullable=True,
                  comment="e.g. 'Individual', 'Company', 'Trust', 'SMSF'"),
    )

    # Soft-link from beneficiary_entitlements rows to a Contact record.
    # Nullable — existing rows keep working; new rows can reference a Contact.
    op.add_column(
        "beneficiary_entitlements",
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_beneficiary_entitlements_contact",
        "beneficiary_entitlements",
        ["contact_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_beneficiary_entitlements_contact", table_name="beneficiary_entitlements")
    op.drop_column("beneficiary_entitlements", "contact_id")
    op.drop_column("contacts", "default_income_classification")
    op.drop_column("contacts", "share_percentage")
    op.drop_column("contacts", "tfn")
    # PostgreSQL does not support removing enum values — downgrade leaves BENEFICIARY
    # in the enum type but it becomes unused.
