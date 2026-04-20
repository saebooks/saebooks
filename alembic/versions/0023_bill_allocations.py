"""Extend payment_allocations with bill_id for OUTGOING payments

Revision ID: 0023_bill_allocations
Revises: 0022_bills
Create Date: 2026-04-21

Batch S already added ``invoice_id`` + ``credit_note_id`` as nullable
FKs on ``payment_allocations``. Batch V adds a sibling ``bill_id`` so
that OUTGOING payments can allocate against AP bills the same way
INCOMING payments allocate against AR invoices.

Exactly one of the three FKs must be set per allocation row — that
rule is enforced in the service layer rather than via a DB CHECK so
that future polymorphic targets (statement lines, credit adjustments,
etc.) can be added without another migration.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_bill_allocations"
down_revision: str | None = "0022_bills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_allocations",
        sa.Column(
            "bill_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bills.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_payment_allocations_bill",
        "payment_allocations",
        ["bill_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payment_allocations_bill", table_name="payment_allocations"
    )
    op.drop_column("payment_allocations", "bill_id")
