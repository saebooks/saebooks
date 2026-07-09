"""Add psi_status column to companies table.

No PSI/80-20 classification surface in
settings or dashboard; contractors could deduct disallowed PSI expenses
without warning. "unsure" default causes a dashboard banner prompting
the operator to classify their business type.

Revision ID: 0073_psi_status
Revises: 0072_contact_currency_code
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0073_psi_status"
down_revision: str | None = "0072_contact_currency_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "psi_status",
            sa.String(16),
            nullable=False,
            server_default="unsure",
        ),
    )


def downgrade() -> None:
    op.drop_column("companies", "psi_status")
