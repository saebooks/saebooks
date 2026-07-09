"""0140_payments_one_off_customer — add one_off_customer_id to payments + repoint orphans.

Rationale (Richard, 2026-05-27):
    0137_one_off_parties added one_off_vendor_id to payments (for OUTGOING
    payments to one-off suppliers) but never added one_off_customer_id (for
    INCOMING payments from one-off customers). When a contact is demoted
    from CUSTOMER → one_off_customer, its incoming payments are left with
    contact_id=NULL and no replacement link — the cash movement is preserved
    in the JE chain, but the payment record loses its party reference.

Architectural change:
    - Add nullable one_off_customer_id FK on payments.
    - Replace 2-way mutex CHECK `ck_payments_party_mutex` with 3-way version
      that allows exactly one of (contact_id, one_off_vendor_id,
      one_off_customer_id) to be set (or all NULL).
    - Index on the new column for lookup parity with one_off_vendor_id.
    - Data repair: for any payment with all three party FKs NULL that has
      a payment_allocations row pointing at an invoice with
      one_off_customer_id set, copy that one_off_customer_id onto the
      payment. This re-establishes the link severed by 0137 demotions.

Tenant-scoping: no new table, no new policy. Existing payments RLS covers it.

Revision ID: 0140_payments_one_off_customer
Revises: 0139_merge_heads
Create Date: 2026-05-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0140_payments_one_off_customer"
down_revision: str | None = "0139_merge_heads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("one_off_customer_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "payments_one_off_customer_id_fkey",
        "payments",
        "one_off_customers",
        ["one_off_customer_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_payments_one_off_customer_id",
        "payments",
        ["one_off_customer_id"],
    )

    op.drop_constraint("ck_payments_party_mutex", "payments", type_="check")
    op.create_check_constraint(
        "ck_payments_party_mutex",
        "payments",
        # at most one of the three party FKs is set per row (all NULL ok)
        "NOT (contact_id IS NOT NULL AND one_off_vendor_id IS NOT NULL) "
        "AND NOT (contact_id IS NOT NULL AND one_off_customer_id IS NOT NULL) "
        "AND NOT (one_off_vendor_id IS NOT NULL AND one_off_customer_id IS NOT NULL)",
    )

    # Repair: any payment with all three party FKs NULL but linked via
    # payment_allocations to an invoice carrying one_off_customer_id —
    # adopt the invoice's one_off_customer_id.
    op.execute("""
        UPDATE payments p
        SET one_off_customer_id = sub.one_off_customer_id
        FROM (
            SELECT DISTINCT ON (pa.payment_id)
                pa.payment_id,
                i.one_off_customer_id
            FROM payment_allocations pa
            JOIN invoices i ON i.id = pa.invoice_id
            WHERE i.one_off_customer_id IS NOT NULL
            ORDER BY pa.payment_id, pa.created_at
        ) sub
        WHERE sub.payment_id = p.id
          AND p.contact_id IS NULL
          AND p.one_off_vendor_id IS NULL
          AND p.one_off_customer_id IS NULL
    """)


def downgrade() -> None:
    # Restore the 2-way mutex (the new column is going away).
    op.drop_constraint("ck_payments_party_mutex", "payments", type_="check")

    # Null out the new column on all rows before dropping (RESTRICT FK would
    # block otherwise).
    op.execute("UPDATE payments SET one_off_customer_id = NULL WHERE one_off_customer_id IS NOT NULL")

    op.drop_index("ix_payments_one_off_customer_id", table_name="payments")
    op.drop_constraint(
        "payments_one_off_customer_id_fkey", "payments", type_="foreignkey"
    )
    op.drop_column("payments", "one_off_customer_id")

    op.create_check_constraint(
        "ck_payments_party_mutex",
        "payments",
        "(contact_id IS NULL OR one_off_vendor_id IS NULL)",
    )
