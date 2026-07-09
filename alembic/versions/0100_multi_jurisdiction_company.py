"""Multi-jurisdiction company-side tables and companies.jurisdiction column.

Adds the three company-DB tables that anchor the engine on a per-company
basis:

* ``tax_periods``       — instances ("AU 2026-Q3 BAS for company X")
* ``tax_returns``       — generated returns linked to a period
* ``lodgement_records`` — receipts of regulator transmission

Also adds ``companies.jurisdiction`` (text NOT NULL DEFAULT 'AU') so
existing rows acquire a sensible default and downstream periodisation
knows which strategy to dispatch.

Migration is intentionally numbered 0100 to leave a buffer between the
landed v0.1.3 quotes work (latest tracked: 0097) and the v0.1.4
multi-jurisdiction wave.

Doubles as an alembic merge node: at the v0.1.3 base commit two heads
share parent 0094_purchase_orders (0095_quotes_tables landed alongside
0095_sync_state_tables without a merge migration). 0100 closes that
gap by listing both as parents.

Revision ID: 0100_multi_jurisdiction_company
Revises: 0097_invoices_source_quote_id, 0095_quotes_tables
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0100_multi_jurisdiction_company"
down_revision: tuple[str, ...] | None = ("0097_invoices_source_quote_id", "0095_quotes_tables")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. companies.jurisdiction — text, not FK (cross-DB).
    #    DEFAULT 'AU' so existing rows acquire the only jurisdiction
    #    that is actually wired end-to-end at v0.1.4.
    op.add_column(
        "companies",
        sa.Column(
            "jurisdiction", sa.String(3), nullable=False, server_default="AU"
        ),
    )

    # 2. tax_periods enums (auto-created by create_table on first use).
    period_type = sa.Enum(
        "monthly", "quarterly", "bimonthly", "six_monthly", "annual",
        name="tax_period_type",
    )
    period_status = sa.Enum(
        "open", "locked", "lodged", name="tax_period_status",
    )

    op.create_table(
        "tax_periods",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text("'00000000-0000-0000-0000-000000000001'::uuid"),
        ),
        sa.Column("jurisdiction", sa.String(3), nullable=False),
        sa.Column("period_type", period_type, nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column(
            "status", period_status, nullable=False, server_default="open",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.UniqueConstraint(
            "company_id", "jurisdiction", "period_start",
            name="uq_tax_periods_company_jur_start",
        ),
    )

    # 3. tax_returns
    return_status = sa.Enum(
        "draft", "ready", "lodged", "accepted", "rejected", "amended",
        name="tax_return_status",
    )

    op.create_table(
        "tax_returns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text("'00000000-0000-0000-0000-000000000001'::uuid"),
        ),
        sa.Column("jurisdiction", sa.String(3), nullable=False),
        sa.Column(
            "period_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_periods.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("return_type", sa.String(32), nullable=False),
        sa.Column("figures", postgresql.JSONB, nullable=False),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.Column(
            "generated_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "status", return_status, nullable=False, server_default="draft",
        ),
        # FK added after lodgement_records exists (USING SET NULL on circular).
        sa.Column("lodgement_record_id", postgresql.UUID(as_uuid=True)),
    )

    # 4. lodgement_records
    lodgement_status = sa.Enum(
        "pending", "submitted", "accepted", "rejected", "error",
        name="lodgement_status",
    )

    op.create_table(
        "lodgement_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            server_default=sa.text("'00000000-0000-0000-0000-000000000001'::uuid"),
        ),
        sa.Column(
            "tax_return_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_returns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("jurisdiction", sa.String(3), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "submitted_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("regulator", sa.String(32), nullable=False),
        sa.Column("regulator_reference", sa.String(128)),
        sa.Column(
            "status", lodgement_status, nullable=False, server_default="pending",
        ),
        sa.Column("request_blob", postgresql.JSONB),
        sa.Column("response_blob", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
    )

    # Now that lodgement_records exists, wire the back-reference FK.
    op.create_foreign_key(
        "fk_tax_returns_lodgement_record",
        "tax_returns", "lodgement_records",
        ["lodgement_record_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tax_returns_lodgement_record", "tax_returns", type_="foreignkey",
    )
    op.drop_table("lodgement_records")
    op.drop_table("tax_returns")
    op.drop_table("tax_periods")
    op.drop_column("companies", "jurisdiction")
    sa.Enum(name="lodgement_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="tax_return_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="tax_period_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="tax_period_type").drop(op.get_bind(), checkfirst=True)
