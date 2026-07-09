"""Add quotes and quote_lines tables.

A Quote is a pre-invoice sales document. It has no GL impact until
converted to an invoice (status INVOICED). See ``saebooks/models/quote.py``
for the full lifecycle and field-level documentation.

Tables created
--------------
* ``quotes`` — header; carries ``tenant_id`` for Class-A RLS.
* ``quote_lines`` — line items; scoped via parent (no direct policy).

RLS
---
``quotes`` gets the standard ``tenant_isolation`` policy with FORCE,
matching the pattern from 0094 (purchase_orders) and 0055.

Indexes
-------
* ``uq_quotes_tenant_number`` UNIQUE (tenant_id, number) — per-tenant
  unique quote numbers.
* ``ix_quotes_tenant_customer_status`` (tenant_id, customer_id, status)
* ``ix_quotes_tenant_status_expiry`` (tenant_id, status, expiry_date)
* ``ix_quote_lines_quote`` (quote_id)

Revision ID: 0095_quotes_tables
Revises: 0094_purchase_orders
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0095_quotes_tables"
down_revision: str | None = "0094_purchase_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUOTE_STATUSES = (
    "DRAFT",
    "SENT",
    "ACCEPTED",
    "DECLINED",
    "ARCHIVED",
    "INVOICED",
)
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    quote_status = postgresql.ENUM(*QUOTE_STATUSES, name="quote_status_enum")
    quote_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "quotes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_TENANT}'"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.String(32)),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                *QUOTE_STATUSES,
                name="quote_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("expiry_date", sa.Date()),
        sa.Column("currency", sa.String(3), nullable=False, server_default="AUD"),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("validity_days", sa.Integer(), nullable=False, server_default="28"),
        sa.Column("deposit_pct", sa.Numeric(6, 2), nullable=False, server_default="50"),
        sa.Column(
            "late_fee_pct_per_month",
            sa.Numeric(6, 4),
            nullable=False,
            server_default="2.5",
        ),
        sa.Column(
            "is_supply_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("notes", sa.Text()),
        sa.Column("terms", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("declined_at", sa.DateTime(timezone=True)),
        sa.Column("invoiced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="SET NULL"),
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
        sa.UniqueConstraint("tenant_id", "number", name="uq_quotes_tenant_number"),
    )

    op.create_index(
        "ix_quotes_tenant_customer_status",
        "quotes",
        ["tenant_id", "customer_id", "status"],
    )
    op.create_index(
        "ix_quotes_tenant_status_expiry",
        "quotes",
        ["tenant_id", "status", "expiry_date"],
    )

    # Class-A RLS: enable + force + tenant_isolation policy.
    op.execute("ALTER TABLE quotes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE quotes FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON quotes "
        f"FOR ALL USING {_USING} WITH CHECK {_USING}"
    )

    op.create_table(
        "quote_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("quotes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column(
            "tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
        ),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_quote_lines_quote",
        "quote_lines",
        ["quote_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quote_lines_quote", table_name="quote_lines")
    op.drop_table("quote_lines")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON quotes")
    op.execute("ALTER TABLE quotes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE quotes DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_quotes_tenant_status_expiry", table_name="quotes")
    op.drop_index("ix_quotes_tenant_customer_status", table_name="quotes")
    op.drop_table("quotes")

    postgresql.ENUM(*QUOTE_STATUSES, name="quote_status_enum").drop(
        op.get_bind(), checkfirst=True
    )
