"""B/48 — add stripe_payment_link column to invoices table.

Stores the Stripe Checkout Session URL generated when a user clicks
"Generate Payment Link" on a POSTED invoice. Nullable — most invoices
won't have a Stripe link (paper cheque, bank transfer, etc.).

Revision ID: 0054_add_stripe_payment_link_to_invoices
Revises: 0053_user_password_hash
Create Date: 2026-04-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054_stripe_payment_link"
down_revision: str | None = "0053_user_password_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _col_exists(table: str, col: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": col},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if not _col_exists("invoices", "stripe_payment_link"):
        op.add_column(
            "invoices",
            sa.Column("stripe_payment_link", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("invoices", "stripe_payment_link")
