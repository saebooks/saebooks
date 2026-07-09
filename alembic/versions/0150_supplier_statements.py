"""Add supplier_statements + supplier_statement_lines for statement reconciliation.

Supplier statements (typically from Paperless) are parsed and reconciled
against the bills in our books. Review-only surface — reconciliation never
posts to the GL.

RLS (Class A — direct tenant_id column) on BOTH tables: ENABLE + FORCE ROW
LEVEL SECURITY + a ``tenant_isolation`` policy using the same
``app.current_tenant`` predicate as 0055/0088. Cross-tenant reads/writes are
blocked at the DB layer even under the runtime ``saebooks_app`` role
(NOBYPASSRLS) and even if router auth is bypassed.

Revision ID: 0150_supplier_statements
Revises: 0149_cashbook_tax_code_mapping
Create Date: 2026-06-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0150_supplier_statements"
down_revision: str | None = "0149_cashbook_tax_code_mapping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088 verbatim).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLES = ("supplier_statements", "supplier_statement_lines")


def _apply_rls(table: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )


def upgrade() -> None:
    op.create_table(
        "supplier_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("supplier_name", sa.Text(), nullable=True),
        sa.Column("supplier_abn", sa.Text(), nullable=True),
        sa.Column("customer_ref", sa.Text(), nullable=True),
        sa.Column("statement_date", sa.Date(), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("opening_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("closing_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="AUD", nullable=False),
        sa.Column("status", sa.String(32),
                  server_default="pending_extract", nullable=False),
        sa.Column("our_ap_as_at", sa.Numeric(18, 2), nullable=True),
        sa.Column("balance_delta", sa.Numeric(18, 2), nullable=True),
        sa.Column("extraction_meta", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("supplier_statements_tenant_idx", "supplier_statements", ["tenant_id"])
    op.create_index("supplier_statements_company_idx", "supplier_statements", ["company_id"])
    op.create_index("supplier_statements_source_doc_idx", "supplier_statements",
                    ["source_document_id"])

    op.create_table(
        "supplier_statement_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("statement_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("supplier_statements.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("line_date", sa.Date(), nullable=True),
        sa.Column("line_type", sa.String(16), server_default="unknown", nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("match_status", sa.String(24),
                  server_default="unmatched", nullable=False),
        sa.Column("matched_bill_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("bills.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("supplier_statement_lines_tenant_idx",
                    "supplier_statement_lines", ["tenant_id"])
    op.create_index("supplier_statement_lines_statement_idx",
                    "supplier_statement_lines", ["statement_id"])

    for t in _TABLES:
        _apply_rls(t)


def downgrade() -> None:
    for t in _TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
        op.execute(sa.text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("supplier_statement_lines")
    op.drop_table("supplier_statements")
