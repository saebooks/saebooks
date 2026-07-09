"""Add franking credit columns to journal_lines, invoice_lines, and distributions.

Franking credits not modelled; a fully-
franked dividend could only record the cash component, losing the imputation
credit.  This migration adds annotation columns so operators can record the
grossed-up income and pass franking credits through to beneficiary statements.

Changes:
- journal_lines:            franking_credit_amount NUMERIC(14,2) NULL
- invoice_lines:            franking_credit_amount NUMERIC(14,2) NULL
                            franking_percentage    NUMERIC(7,4)  NULL
- trust_distributions:      total_franking_credits NUMERIC(14,2) NOT NULL DEFAULT 0
- beneficiary_entitlements: franking_credit_amount NUMERIC(14,2) NOT NULL DEFAULT 0

Revision ID: 0061_franking_credits
Revises:     0060_beneficiary_contact_type
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0061_franking_credits"
down_revision: str | None = "0060_beneficiary_contact_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "journal_lines",
        sa.Column("franking_credit_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("franking_credit_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("franking_percentage", sa.Numeric(7, 4), nullable=True),
    )
    op.add_column(
        "trust_distributions",
        sa.Column(
            "total_franking_credits",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "beneficiary_entitlements",
        sa.Column(
            "franking_credit_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("beneficiary_entitlements", "franking_credit_amount")
    op.drop_column("trust_distributions", "total_franking_credits")
    op.drop_column("invoice_lines", "franking_percentage")
    op.drop_column("invoice_lines", "franking_credit_amount")
    op.drop_column("journal_lines", "franking_credit_amount")
