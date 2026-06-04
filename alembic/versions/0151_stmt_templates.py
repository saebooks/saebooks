"""Add supplier_statement_templates — per-supplier extraction hints (P4, #28).

RLS Class A (direct tenant_id): ENABLE + FORCE ROW LEVEL SECURITY + the
``tenant_isolation`` policy (same ``app.current_tenant`` predicate as
0055/0088/0150). Review-only metadata; no GL impact.

Revision ID: 0151_stmt_templates
Revises: 0150_supplier_statements
Create Date: 2026-06-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0151_stmt_templates"
down_revision: str | None = "0150_supplier_statements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    op.create_table(
        "supplier_statement_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("supplier_abn", sa.String(20), nullable=True),
        sa.Column("supplier_name", sa.Text(), nullable=True),
        sa.Column("prompt_hint", sa.Text(), nullable=False),
        sa.Column("page_scope", sa.String(32), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("supplier_statement_templates_tenant_idx",
                    "supplier_statement_templates", ["tenant_id"])
    op.create_index("supplier_statement_templates_company_idx",
                    "supplier_statement_templates", ["company_id"])
    op.create_index("supplier_statement_templates_contact_idx",
                    "supplier_statement_templates", ["contact_id"])
    op.create_index("supplier_statement_templates_abn_idx",
                    "supplier_statement_templates", ["supplier_abn"])

    op.execute(sa.text("ALTER TABLE supplier_statement_templates ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE supplier_statement_templates FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON supplier_statement_templates"))
    op.execute(sa.text(
        "CREATE POLICY tenant_isolation ON supplier_statement_templates "
        f"FOR ALL USING {_USING} WITH CHECK {_USING}"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON supplier_statement_templates"))
    op.execute(sa.text("ALTER TABLE supplier_statement_templates NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("supplier_statement_templates")
