"""Expenses + expense_lines — paid-at-checkout sibling of bills.

Sibling of ``0022_bills`` plus ``0094_purchase_orders`` modern
conventions in one shot. Class-A RLS: ``expenses`` carries
``tenant_id`` directly so the ``tenant_isolation`` policy from 0055
applies verbatim. ``expense_lines`` is parent-scoped (mirrors how
``bill_lines`` are handled — gated by joining through the parent in
every service-layer query).

Revision ID: 0101_expenses
Revises: 0100_multi_jurisdiction_company
Create Date: 2026-05-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0101_expenses"
down_revision: str | None = "0100_multi_jurisdiction_company"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPENSE_STATUSES = ("DRAFT", "POSTED", "VOIDED")
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    expense_status = postgresql.ENUM(*EXPENSE_STATUSES, name="expense_status_enum")
    expense_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "expenses",
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
        ),
        sa.Column(
            "payment_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number", sa.String(32)),
        sa.Column("reference", sa.String(64)),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *EXPENSE_STATUSES,
                name="expense_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="AUD"),
        sa.Column("fx_rate", sa.Numeric(18, 8), nullable=False, server_default="1"),
        sa.Column("base_subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("base_tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("base_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
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
        sa.Column("external_id", sa.String(255)),
        sa.Column("external_source", sa.String(64)),
        sa.Column("external_etag", sa.String(255)),
        sa.Column("external_payload", postgresql.JSONB()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_TENANT}'"),
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
            "company_id", "number", name="uq_expenses_company_number"
        ),
    )
    op.create_index(
        "ix_expenses_company_status",
        "expenses",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_expenses_company_contact",
        "expenses",
        ["company_id", "contact_id"],
    )
    op.create_index(
        "ix_expenses_company_date",
        "expenses",
        ["company_id", "expense_date"],
    )
    op.create_index(
        "ix_expenses_payment_account",
        "expenses",
        ["company_id", "payment_account_id"],
    )
    op.create_index(
        "ix_expenses_external",
        "expenses",
        ["company_id", "external_source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    # Class-A RLS: enable + force + tenant_isolation policy.
    op.execute("ALTER TABLE expenses ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE expenses FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON expenses "
        f"FOR ALL USING {_USING} WITH CHECK {_USING}"
    )

    op.create_table(
        "expense_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "expense_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("expenses.id", ondelete="CASCADE"),
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
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_expense_lines_expense",
        "expense_lines",
        ["expense_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_expense_lines_expense", table_name="expense_lines")
    op.drop_table("expense_lines")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON expenses")
    op.execute("ALTER TABLE expenses NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE expenses DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_expenses_external", table_name="expenses")
    op.drop_index("ix_expenses_payment_account", table_name="expenses")
    op.drop_index("ix_expenses_company_date", table_name="expenses")
    op.drop_index("ix_expenses_company_contact", table_name="expenses")
    op.drop_index("ix_expenses_company_status", table_name="expenses")
    op.drop_table("expenses")

    postgresql.ENUM(
        *EXPENSE_STATUSES, name="expense_status_enum"
    ).drop(op.get_bind(), checkfirst=True)
