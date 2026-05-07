"""Multi-tenant foundation — add tenant_id to all entity tables + RLS.

Adds ``tenant_id UUID NOT NULL`` (initially with a default so the
backfill works) to: contacts, accounts, companies, tax_codes, users,
items.  Then drops the server default so future rows must supply it.

Also enables Postgres Row-Level Security on every entity table and
creates a tenant_isolation policy that gates reads/writes through the
``app.current_tenant`` session-local variable set by the API auth
middleware.

Revision ID: 0041_add_tenant_id
Revises: 0040_tenants
Create Date: 2026-04-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0041_add_tenant_id"
down_revision: str | None = "0040_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"

# Tables that get tenant_id + RLS
_ENTITY_TABLES = [
    "contacts",
    "accounts",
    "companies",
    "tax_codes",
    "users",
    "items",
]


def upgrade() -> None:
    for table in _ENTITY_TABLES:
        # 1. Add column with a server default so the backfill INSERT works.
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                PG_UUID(as_uuid=False),
                nullable=False,
                server_default=sa.text(f"'{_DEFAULT_TENANT}'"),
            ),
        )
        # 2. Backfill existing rows.
        op.execute(
            sa.text(
                f"UPDATE {table} SET tenant_id = '{_DEFAULT_TENANT}'"  # noqa: S608
            )
        )
        # 3. Add FK constraint.
        op.create_foreign_key(
            f"fk_{table}_tenant_id",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        # 4. Drop server default so future INSERTs must supply tenant_id.
        op.alter_column(table, "tenant_id", server_default=None)

    # ------------------------------------------------------------------ #
    # Postgres Row-Level Security                                          #
    # ------------------------------------------------------------------ #
    # Note: we do NOT use FORCE ROW LEVEL SECURITY — that would also
    # apply RLS to the table owner (saebooks user), breaking migrations
    # and direct-DB tooling.  Application users that are *not* the
    # table owner will be subject to RLS normally.  Phase 2 will create
    # a dedicated ``saebooks_app`` role with no BYPASSRLS privilege and
    # connect via that role, at which point FORCE can be re-enabled.
    for table in _ENTITY_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (tenant_id = current_setting('app.current_tenant', true)::uuid)"
            )
        )


def downgrade() -> None:
    for table in _ENTITY_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")
