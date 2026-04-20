"""AP bills + bill lines

Revision ID: 0022_bills
Revises: 0021_recurring_invoices
Create Date: 2026-04-21

Mirror of the invoices table from 0019 with two bill-specific additions:

* ``supplier_reference`` — the supplier's own invoice number (their
  reference that goes on our remittance advice). Distinct from our
  internal ``number`` (``BILL-000042``).
* ``amount_paid`` tracks how much of the bill has been settled by
  outgoing payments (same shape as the AR side).

Nothing about payments or allocations is introduced here — those
already exist from Batch S and work with ``direction='OUTGOING'``
against AP.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022_bills"
down_revision: str | None = "0021_recurring_invoices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BILL_STATUSES = ("DRAFT", "POSTED", "VOIDED")


def upgrade() -> None:
    bill_status = postgresql.ENUM(*BILL_STATUSES, name="bill_status_enum")
    bill_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "bills",
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
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number", sa.String(32)),
        sa.Column("supplier_reference", sa.String(64)),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*BILL_STATUSES, name="bill_status_enum", create_type=False),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("amount_paid", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("posted_by", sa.String()),
        sa.Column(
            "journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "void_journal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        ),
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
        sa.UniqueConstraint("company_id", "number", name="uq_bills_company_number"),
    )
    op.create_index(
        "ix_bills_company_status",
        "bills",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_bills_company_contact",
        "bills",
        ["company_id", "contact_id"],
    )
    op.create_index(
        "ix_bills_company_due",
        "bills",
        ["company_id", "due_date"],
    )

    op.create_table(
        "bill_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "bill_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
        ),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("discount_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("line_subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("line_tax", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_bill_lines_bill",
        "bill_lines",
        ["bill_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_bill_lines_bill", table_name="bill_lines")
    op.drop_table("bill_lines")
    op.drop_index("ix_bills_company_due", table_name="bills")
    op.drop_index("ix_bills_company_contact", table_name="bills")
    op.drop_index("ix_bills_company_status", table_name="bills")
    op.drop_table("bills")
    postgresql.ENUM(*BILL_STATUSES, name="bill_status_enum").drop(
        op.get_bind(), checkfirst=True
    )
