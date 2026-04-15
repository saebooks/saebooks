"""bank statement lines for reconciliation

Revision ID: 0007_bank_statement_lines
Revises: 0006_journal_templates
Create Date: 2026-04-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_bank_statement_lines"
down_revision: str | None = "0006_journal_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bank_statement_lines",
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
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            comment="The bank/cash account this line belongs to",
        ),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "amount",
            sa.Numeric(14, 2),
            nullable=False,
            comment="Positive=deposit, negative=withdrawal",
        ),
        sa.Column("reference", sa.String(128)),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="UNMATCHED",
        ),
        sa.Column(
            "matched_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            comment="The journal entry this line was reconciled against",
        ),
        sa.Column("matched_at", sa.DateTime(timezone=True)),
        sa.Column("matched_by", sa.String()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_bank_statement_lines_account_status",
        "bank_statement_lines",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_bank_statement_lines_company_id",
        "bank_statement_lines",
        ["company_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_bank_statement_lines_company_id", table_name="bank_statement_lines")
    op.drop_index("ix_bank_statement_lines_account_status", table_name="bank_statement_lines")
    op.drop_table("bank_statement_lines")
