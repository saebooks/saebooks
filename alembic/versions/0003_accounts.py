"""accounts table + raw AU reference tables

Revision ID: 0003_accounts
Revises: 0002_companies
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_accounts"
down_revision: str | None = "0002_companies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACCOUNT_TYPES = (
    "ASSET",
    "LIABILITY",
    "EQUITY",
    "INCOME",
    "OTHER_INCOME",
    "EXPENSE",
    "COST_OF_SALES",
    "OTHER_EXPENSE",
)


def upgrade() -> None:
    account_type_enum = postgresql.ENUM(*ACCOUNT_TYPES, name="account_type_enum")
    account_type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "accounts",
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
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "account_type",
            postgresql.ENUM(*ACCOUNT_TYPES, name="account_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("tax_code_default", sa.String()),
        sa.Column("is_header", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reconcile", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("company_id", "code", name="uq_accounts_company_code"),
    )
    op.create_index("ix_accounts_company_id", "accounts", ["company_id"])
    op.create_index("ix_accounts_account_type", "accounts", ["account_type"])

    # Raw reference tables — shape-only, modelled later
    for raw_name in (
        "raw_au_tax_codes",
        "raw_au_tax_groups",
        "raw_au_fiscal_positions",
        "raw_au_account_tags",
        "raw_au_depreciation_models",
    ):
        op.create_table(
            raw_name,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        )


def downgrade() -> None:
    for raw_name in (
        "raw_au_tax_codes",
        "raw_au_tax_groups",
        "raw_au_fiscal_positions",
        "raw_au_account_tags",
        "raw_au_depreciation_models",
    ):
        op.drop_table(raw_name)
    op.drop_index("ix_accounts_account_type", table_name="accounts")
    op.drop_index("ix_accounts_company_id", table_name="accounts")
    op.drop_table("accounts")
    postgresql.ENUM(name="account_type_enum").drop(op.get_bind(), checkfirst=True)
