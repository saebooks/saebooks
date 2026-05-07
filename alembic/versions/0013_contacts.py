"""contacts table and link from bank statement lines

Revision ID: 0013_contacts
Revises: 0012_sub_headers
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_contacts"
down_revision: str | None = "0012_sub_headers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTACT_TYPES = ("CUSTOMER", "SUPPLIER", "BOTH")


def upgrade() -> None:
    contact_type_enum = postgresql.ENUM(*CONTACT_TYPES, name="contact_type_enum")
    contact_type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "contacts",
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
        sa.Column(
            "contact_type",
            postgresql.ENUM(*CONTACT_TYPES, name="contact_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("email", sa.String()),
        sa.Column("phone", sa.String(32)),
        sa.Column(
            "abn",
            sa.String(14),
            comment="Australian Business Number — 11 digits stored as 'xx xxx xxx xxx'",
        ),
        sa.Column("address_line1", sa.String()),
        sa.Column("address_line2", sa.String()),
        sa.Column("city", sa.String()),
        sa.Column("state", sa.String(8), comment="AU state code e.g. NSW, VIC, QLD"),
        sa.Column("postcode", sa.String(8)),
        sa.Column("country", sa.String(64), server_default="Australia"),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "default_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
        ),
        sa.Column("default_tax_code", sa.String(16)),
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
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_contacts_company_name",
        "contacts",
        ["company_id", "name"],
    )
    op.create_index(
        "ix_contacts_company_type",
        "contacts",
        ["company_id", "contact_type"],
    )

    # Add contact_id FK to bank_statement_lines
    op.add_column(
        "bank_statement_lines",
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bank_statement_lines", "contact_id")
    op.drop_index("ix_contacts_company_type", table_name="contacts")
    op.drop_index("ix_contacts_company_name", table_name="contacts")
    op.drop_table("contacts")
    postgresql.ENUM(*CONTACT_TYPES, name="contact_type_enum").drop(
        op.get_bind(), checkfirst=True
    )
