"""Payments + credit notes.

Revision ID: 0020_payments_credit_notes
Revises: 0019_invoices
Create Date: 2026-04-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020_payments_credit_notes"
down_revision: str | None = "0019_invoices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYMENT_STATUSES = ("DRAFT", "POSTED", "VOIDED")
PAYMENT_DIRECTIONS = ("INCOMING", "OUTGOING")
PAYMENT_METHODS = (
    "cash",
    "eft",
    "cheque",
    "card",
    "direct_deposit",
    "other",
)
CREDIT_NOTE_STATUSES = ("DRAFT", "POSTED", "VOIDED")


def upgrade() -> None:
    payment_status = postgresql.ENUM(*PAYMENT_STATUSES, name="payment_status_enum")
    payment_status.create(op.get_bind(), checkfirst=True)
    payment_dir = postgresql.ENUM(
        *PAYMENT_DIRECTIONS, name="payment_direction_enum"
    )
    payment_dir.create(op.get_bind(), checkfirst=True)
    payment_method = postgresql.ENUM(*PAYMENT_METHODS, name="payment_method_enum")
    payment_method.create(op.get_bind(), checkfirst=True)
    credit_status = postgresql.ENUM(
        *CREDIT_NOTE_STATUSES, name="credit_note_status_enum"
    )
    credit_status.create(op.get_bind(), checkfirst=True)

    # ---------------- credit_notes (create first so payment_allocations
    # can FK to it) ---------------- #
    op.create_table(
        "credit_notes",
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
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *CREDIT_NOTE_STATUSES,
                name="credit_note_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column(
            "original_invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="SET NULL"),
        ),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("amount_allocated", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text()),
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
        sa.UniqueConstraint(
            "company_id", "number", name="uq_credit_notes_company_number"
        ),
    )
    op.create_index(
        "ix_credit_notes_company_status", "credit_notes", ["company_id", "status"]
    )
    op.create_index(
        "ix_credit_notes_company_contact",
        "credit_notes",
        ["company_id", "contact_id"],
    )

    # ---------------- credit_note_lines ---------------- #
    op.create_table(
        "credit_note_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "credit_note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credit_notes.id", ondelete="CASCADE"),
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
        "ix_credit_note_lines_note", "credit_note_lines", ["credit_note_id"]
    )

    # ---------------- payments ---------------- #
    op.create_table(
        "payments",
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
        sa.Column(
            "bank_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number", sa.String(32)),
        sa.Column(
            "direction",
            postgresql.ENUM(
                *PAYMENT_DIRECTIONS,
                name="payment_direction_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "method",
            postgresql.ENUM(
                *PAYMENT_METHODS, name="payment_method_enum", create_type=False
            ),
            nullable=False,
            server_default="eft",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                *PAYMENT_STATUSES,
                name="payment_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("reference", sa.String(128)),
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
        sa.UniqueConstraint("company_id", "number", name="uq_payments_company_number"),
    )
    op.create_index("ix_payments_company_status", "payments", ["company_id", "status"])
    op.create_index(
        "ix_payments_company_contact", "payments", ["company_id", "contact_id"]
    )
    op.create_index("ix_payments_company_date", "payments", ["company_id", "payment_date"])

    # ---------------- payment_allocations ---------------- #
    op.create_table(
        "payment_allocations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "credit_note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credit_notes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_payment_allocations_payment", "payment_allocations", ["payment_id"]
    )
    op.create_index(
        "ix_payment_allocations_invoice", "payment_allocations", ["invoice_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payment_allocations_invoice", table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_payment", table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_index("ix_payments_company_date", table_name="payments")
    op.drop_index("ix_payments_company_contact", table_name="payments")
    op.drop_index("ix_payments_company_status", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_credit_note_lines_note", table_name="credit_note_lines")
    op.drop_table("credit_note_lines")
    op.drop_index("ix_credit_notes_company_contact", table_name="credit_notes")
    op.drop_index("ix_credit_notes_company_status", table_name="credit_notes")
    op.drop_table("credit_notes")
    for enum_name in (
        "credit_note_status_enum",
        "payment_method_enum",
        "payment_direction_enum",
        "payment_status_enum",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
