"""Recurring invoice templates.

Revision ID: 0021_recurring_invoices
Revises: 0020_payments_credit_notes
Create Date: 2026-04-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021_recurring_invoices"
down_revision: str | None = "0020_payments_credit_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECURRENCE_FREQUENCIES = (
    "WEEKLY",
    "FORTNIGHTLY",
    "MONTHLY",
    "QUARTERLY",
    "YEARLY",
)
RECURRENCE_STATUSES = ("ACTIVE", "PAUSED", "ENDED")


def upgrade() -> None:
    freq = postgresql.ENUM(
        *RECURRENCE_FREQUENCIES, name="recurrence_frequency_enum"
    )
    freq.create(op.get_bind(), checkfirst=True)
    status = postgresql.ENUM(
        *RECURRENCE_STATUSES, name="recurrence_status_enum"
    )
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "recurring_invoices",
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
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "frequency",
            postgresql.ENUM(
                *RECURRENCE_FREQUENCIES,
                name="recurrence_frequency_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                *RECURRENCE_STATUSES,
                name="recurrence_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="ACTIVE",
        ),
        # anchor_day_of_month lets us round-trip e.g. 31-Jan -> 28-Feb
        # -> 31-Mar without drifting. Nullable so WEEKLY/FORTNIGHTLY
        # can skip it.
        sa.Column("anchor_day", sa.Integer()),
        sa.Column("next_run", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date()),
        sa.Column("last_run", sa.Date()),
        sa.Column(
            "due_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column("payment_terms", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("auto_post", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "invoices_generated",
            sa.Integer(),
            nullable=False,
            server_default="0",
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
    )
    op.create_index(
        "ix_recurring_invoices_company",
        "recurring_invoices",
        ["company_id"],
    )
    op.create_index(
        "ix_recurring_invoices_next_run",
        "recurring_invoices",
        ["next_run", "status"],
    )

    op.create_table(
        "recurring_invoice_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "recurring_invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("recurring_invoices.id", ondelete="CASCADE"),
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
        sa.Column(
            "quantity", sa.Numeric(18, 4), nullable=False, server_default="1"
        ),
        sa.Column(
            "unit_price", sa.Numeric(18, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "discount_pct", sa.Numeric(6, 2), nullable=False, server_default="0"
        ),
    )
    op.create_index(
        "ix_recurring_invoice_lines_parent",
        "recurring_invoice_lines",
        ["recurring_invoice_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recurring_invoice_lines_parent",
        table_name="recurring_invoice_lines",
    )
    op.drop_table("recurring_invoice_lines")
    op.drop_index(
        "ix_recurring_invoices_next_run", table_name="recurring_invoices"
    )
    op.drop_index(
        "ix_recurring_invoices_company", table_name="recurring_invoices"
    )
    op.drop_table("recurring_invoices")
    for enum_name in ("recurrence_status_enum", "recurrence_frequency_enum"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
