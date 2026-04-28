"""Add is_tpar_supplier flag to contacts table.

Gap CIVL-5 (medium-civil-contractor): no TPAR flag on contacts; civil
contractors cannot generate ATO-ready sub-contractor payment report.

Revision ID: 0074_tpar_supplier_flag
Revises: 0073_psi_status
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0074_tpar_supplier_flag"
down_revision: str | None = "0073_psi_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "is_tpar_supplier",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("contacts", "is_tpar_supplier")
