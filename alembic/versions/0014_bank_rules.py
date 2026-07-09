"""bank rules for auto-categorising statement lines

Revision ID: 0014_bank_rules
Revises: 0013_contacts
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_bank_rules"
down_revision: str | None = "0013_contacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MATCH_TYPES = ("CONTAINS", "STARTS_WITH", "EXACT", "REGEX")


def upgrade() -> None:
    match_type_enum = postgresql.ENUM(*MATCH_TYPES, name="match_type_enum")
    match_type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "bank_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("match_pattern", sa.String(), nullable=False),
        sa.Column(
            "match_type",
            postgresql.ENUM(*MATCH_TYPES, name="match_type_enum", create_type=False),
            nullable=False,
            server_default="CONTAINS",
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tax_code", sa.String(16)),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
        ),
        sa.Column("description_template", sa.String()),
        sa.Column("auto_create", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_bank_rules_company_active",
        "bank_rules",
        ["company_id", "is_active"],
    )

    # Add bank_rule_id FK to bank_statement_lines
    op.add_column(
        "bank_statement_lines",
        sa.Column(
            "bank_rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bank_rules.id", ondelete="SET NULL"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bank_statement_lines", "bank_rule_id")
    op.drop_index("ix_bank_rules_company_active", table_name="bank_rules")
    op.drop_table("bank_rules")
    postgresql.ENUM(*MATCH_TYPES, name="match_type_enum").drop(
        op.get_bind(), checkfirst=True
    )
