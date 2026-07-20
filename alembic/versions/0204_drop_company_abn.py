"""Drop companies.abn — business_identifiers is the sole source of truth (K6).

The legacy ``companies.abn`` column overloaded two meanings: an Australian
Business Number for AU companies, and the Estonian äriregistri kood (registry
code) for EE companies. 0145 first mirrored the column into
``business_identifiers`` (scheme ``au_abn``), but the column stayed the
authoritative write target ever since, so any company created/updated after
0145 has a stale or absent identifier row.

This migration re-syncs every company's *current* ``abn`` value into its
correctly-typed scheme — AU (and the historical AU default) -> ``au_abn``,
EE -> ``ee_regcode`` — then drops the column. The column value wins at
cutover (``ON CONFLICT ... DO UPDATE``), matching 0145's "the column is
authoritative for callers" posture. The ORM now exposes ``Company.abn`` as a
read-through hybrid over the ``au_abn`` identifier, and EE consumers read
``ee_regcode`` explicitly, so the historical overload is gone from storage.

The migration role bypasses FORCE RLS (same posture as 0145's backfill and
0181's ``UPDATE business_identifiers``), so the INSERT..SELECT runs without an
``app.current_tenant`` context.

Revision ID: 0204_drop_company_abn
Revises: 0202_money_numeric_18_4
Create Date: 2026-07-12
"""
import sqlalchemy as sa

from alembic import op

revision: str = "0204_drop_company_abn"
down_revision: str | None = "0202_money_numeric_18_4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # EE companies: the overloaded ``abn`` value IS the äriregistri kood.
    op.execute(
        sa.text(
            "INSERT INTO business_identifiers "
            "  (id, company_id, tenant_id, scheme, value, jurisdiction, "
            "   created_at, updated_at) "
            "SELECT gen_random_uuid(), c.id, c.tenant_id, 'ee_regcode', c.abn, "
            "       'EST', NOW(), NOW() "
            "FROM companies c "
            "WHERE c.abn IS NOT NULL AND c.abn <> '' "
            "  AND upper(c.jurisdiction) IN ('EE', 'EST') "
            "ON CONFLICT (company_id, scheme) "
            "  DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        )
    )
    # Everyone else (AU + the historical AU default) -> au_abn.
    op.execute(
        sa.text(
            "INSERT INTO business_identifiers "
            "  (id, company_id, tenant_id, scheme, value, jurisdiction, "
            "   created_at, updated_at) "
            "SELECT gen_random_uuid(), c.id, c.tenant_id, 'au_abn', c.abn, "
            "       'AUS', NOW(), NOW() "
            "FROM companies c "
            "WHERE c.abn IS NOT NULL AND c.abn <> '' "
            "  AND upper(c.jurisdiction) NOT IN ('EE', 'EST') "
            "ON CONFLICT (company_id, scheme) "
            "  DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        )
    )
    op.drop_column("companies", "abn")


def downgrade() -> None:
    op.add_column(
        "companies", sa.Column("abn", sa.String(length=20), nullable=True)
    )
    # Restore the column from whichever primary registry identifier the
    # company carries, mirroring the upgrade's jurisdiction split.
    op.execute(
        sa.text(
            "UPDATE companies c SET abn = bi.value "
            "FROM business_identifiers bi "
            "WHERE bi.company_id = c.id AND bi.scheme = 'au_abn' "
            "  AND upper(c.jurisdiction) NOT IN ('EE', 'EST')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE companies c SET abn = bi.value "
            "FROM business_identifiers bi "
            "WHERE bi.company_id = c.id AND bi.scheme = 'ee_regcode' "
            "  AND upper(c.jurisdiction) IN ('EE', 'EST')"
        )
    )
