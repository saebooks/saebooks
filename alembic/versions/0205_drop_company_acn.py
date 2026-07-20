"""Drop companies.acn — business_identifiers is the sole source of truth.

Mirrors the ``abn`` clean-move (0204). The ACN (Australian Company Number) is
AU-specific, so — unlike the overloaded ``abn`` — there is a single target
scheme ``au_acn`` and no jurisdiction split. Backfill every current
``companies.acn`` value into ``au_acn`` (column wins at cutover), then drop the
column. The ORM exposes ``Company.acn`` as a read-through hybrid over the
``au_acn`` identifier.

The migration role bypasses FORCE RLS (same posture as 0145's backfill and
0204), so the INSERT..SELECT runs without an ``app.current_tenant`` context.

Revision ID: 0205_drop_company_acn
Revises: 0204_drop_company_abn
Create Date: 2026-07-12
"""
import sqlalchemy as sa

from alembic import op

revision: str = "0205_drop_company_acn"
down_revision: str | None = "0204_drop_company_abn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO business_identifiers "
            "  (id, company_id, tenant_id, scheme, value, jurisdiction, "
            "   created_at, updated_at) "
            "SELECT gen_random_uuid(), c.id, c.tenant_id, 'au_acn', c.acn, "
            "       'AUS', NOW(), NOW() "
            "FROM companies c "
            "WHERE c.acn IS NOT NULL AND c.acn <> '' "
            "ON CONFLICT (company_id, scheme) "
            "  DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        )
    )
    op.drop_column("companies", "acn")


def downgrade() -> None:
    op.add_column(
        "companies", sa.Column("acn", sa.String(length=20), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE companies c SET acn = bi.value "
            "FROM business_identifiers bi "
            "WHERE bi.company_id = c.id AND bi.scheme = 'au_acn'"
        )
    )
