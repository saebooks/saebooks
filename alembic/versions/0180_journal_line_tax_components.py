"""journal_line_tax_components child table (M1.5 · T2).

Normalises per-line tax into 1:many component rows so co-existing taxes
on one line are first-class queryable data (India CGST+SGST, US
state+county+city, excise-then-VAT, reverse-charge output/input) rather
than a single ``gst_amount`` scalar + a ``tax_treatment`` JSONB blob.

Tenant-scoped: carries company_id + tenant_id (denormalised from the
parent line/entry) and full Row-Level Security, per the project's
new-tenant-table checklist — even though the parent journal_lines relies
on cascade + company_id, this adds defence-in-depth for GL tax data.

See docs/multi-jurisdiction.md (M1.5) (theme T2).

Revision ID: 0180_journal_line_tax_components
Revises: 0179_tax_code_tax_family
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0180_journal_line_tax_components"
down_revision: str | None = "0179_tax_code_tax_family"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "journal_line_tax_components",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "journal_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journal_lines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tax_family", sa.String(16), nullable=False),
        sa.Column(
            "component_role",
            sa.String(32),
            nullable=False,
            server_default="standard",
        ),
        sa.Column("ref_tax_code", sa.String(32)),
        sa.Column(
            "rate_applied", sa.Numeric(9, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "base_amount", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "tax_amount", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "direction", sa.String(8), nullable=False, server_default="none"
        ),
        sa.Column("sequence", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_journal_line_tax_components_line_id",
        "journal_line_tax_components",
        ["journal_line_id"],
    )
    op.create_index(
        "ix_journal_line_tax_components_tenant_id",
        "journal_line_tax_components",
        ["tenant_id"],
    )

    # RLS checklist (same posture as 0145_business_identifiers).
    op.execute(
        sa.text(
            "ALTER TABLE journal_line_tax_components ENABLE ROW LEVEL SECURITY"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE journal_line_tax_components FORCE ROW LEVEL SECURITY"
        )
    )
    op.execute(
        sa.text(
            "DROP POLICY IF EXISTS tenant_isolation ON journal_line_tax_components"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON journal_line_tax_components "
            "FOR ALL "
            "USING (tenant_id = current_setting('app.current_tenant', true)::uuid) "
            "WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid)"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP POLICY IF EXISTS tenant_isolation ON journal_line_tax_components"
        )
    )
    op.drop_index(
        "ix_journal_line_tax_components_tenant_id",
        table_name="journal_line_tax_components",
    )
    op.drop_index(
        "ix_journal_line_tax_components_line_id",
        table_name="journal_line_tax_components",
    )
    op.drop_table("journal_line_tax_components")
