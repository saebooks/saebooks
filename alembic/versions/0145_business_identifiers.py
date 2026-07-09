"""business_identifiers child table — generalises Company.abn / .acn.

Adds a per-tenant child table that decouples the identifier kind
("au_abn", "uk_crn", ...) from the column shape on companies. Future
jurisdictions hang their identifiers off this table without column
migrations on companies.

The legacy ``companies.abn`` column is kept (read-through) so existing
code paths continue to work; the column is dropped in a later
migration once all callers have moved over. Existing non-NULL
``companies.abn`` rows are mirrored into the child table with
``scheme='au_abn'``.

Revision ID: 0101_business_identifiers
Revises: 0100_multi_jurisdiction_company
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0145_business_identifiers"
down_revision: str | None = "0144_multijurisdiction_rls"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "business_identifiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
            server_default=sa.text(f"'{_DEFAULT_TENANT}'::uuid"),
        ),
        sa.Column("scheme", sa.String(32), nullable=False),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "company_id", "scheme", name="uq_business_identifiers_company_scheme"
        ),
    )

    op.create_index(
        "ix_business_identifiers_company_id",
        "business_identifiers",
        ["company_id"],
    )
    op.create_index(
        "ix_business_identifiers_tenant_id",
        "business_identifiers",
        ["tenant_id"],
    )

    # RLS — same shape as 0055/0083: tenant_isolation policy keyed on
    # the request-scoped ``app.current_tenant`` GUC. The default
    # tenant_isolation policy fires for every statement; bypass uses
    # the existing ``saebooks_admin`` role pattern from 0055.
    op.execute(
        sa.text("ALTER TABLE business_identifiers ENABLE ROW LEVEL SECURITY")
    )
    op.execute(
        sa.text("ALTER TABLE business_identifiers FORCE ROW LEVEL SECURITY")
    )
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON business_identifiers")
    )
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON business_identifiers "
            "FOR ALL "
            "USING (tenant_id = current_setting('app.current_tenant', true)::uuid) "
            "WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid)"
        )
    )

    # Backfill: mirror every non-NULL companies.abn into the child
    # table with scheme='au_abn'. Skip rows that already have an entry
    # (idempotent if the migration is re-run after a partial failure).
    op.execute(
        sa.text(
            "INSERT INTO business_identifiers "
            "  (id, company_id, tenant_id, scheme, value, created_at, updated_at) "
            "SELECT gen_random_uuid(), c.id, c.tenant_id, 'au_abn', c.abn, NOW(), NOW() "
            "FROM companies c "
            "WHERE c.abn IS NOT NULL "
            "  AND c.abn <> '' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM business_identifiers bi "
            "    WHERE bi.company_id = c.id AND bi.scheme = 'au_abn'"
            "  )"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON business_identifiers")
    )
    op.drop_index(
        "ix_business_identifiers_tenant_id", table_name="business_identifiers"
    )
    op.drop_index(
        "ix_business_identifiers_company_id", table_name="business_identifiers"
    )
    op.drop_table("business_identifiers")
