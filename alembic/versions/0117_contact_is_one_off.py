"""Add is_one_off flag to contacts table.

Completes the one-off/walk-in contact feature started in the schema
layer (ContactBase.is_one_off) but never wired to the model or DB.
Without this migration, every POST /api/v1/contacts raises 500 because
SQLAlchemy tries to set a column that does not exist.

Fix: add the boolean column with a server-side default of FALSE so
existing rows are backfilled atomically and no data migration is needed.

Revision ID: 0117_contact_is_one_off
Revises: 0116_merge_payroll_into_main
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0117_contact_is_one_off"
down_revision: str | None = "0116_merge_payroll_into_main"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "is_one_off",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("contacts", "is_one_off")
