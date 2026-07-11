"""roles — tenant-scoped, renameable roles (granular_permissions, D2).

Tenant-scoping checklist (see new-table-rls-checklist):
[x] tenant_id NOT NULL column
[x] FK to tenants(id)
[x] ENABLE ROW LEVEL SECURITY + FORCE ROW LEVEL SECURITY
[x] CREATE POLICY tenant_isolation (USING + WITH CHECK)
[x] Index on (tenant_id, ...)
[x] Service-layer filter as defence-in-depth (services/roles.py,
    services/permissions.py — every query filters by tenant_id even
    though RLS would already catch a miss)
[x] Always-set on writes (services/roles.py stamps tenant_id from the
    request-scoped session; never left to a default)
[x] Cross-tenant probe test added (tests/test_rls_roles.py)
[x] RLS probe test added (same file)

What this migration does
-------------------------
1. Creates ``roles`` (id, tenant_id, name, base_role, is_system,
   created_at, updated_at). ``base_role`` is nullable and CHECK-
   constrained to the five legacy ``UserRole`` values when set — see
   ``models/role.py`` docstring for why (bridges the new tenant-scoped
   role to the old rank-based ``users.role`` string for the five
   starter roles that have a legacy equivalent; NULL for Payroll-only
   and any genuinely custom role).
2. Two uniqueness constraints: (tenant_id, name) — no duplicate role
   name within a tenant (renaming must stay collision-free) — and a
   PARTIAL unique index (tenant_id, base_role) WHERE base_role IS NOT
   NULL — at most one role per legacy slot per tenant, which is what
   makes ``resolve_permissions``'s "find the tenant's row for
   base_role=X" lookup deterministic.
3. Backfills the six starter roles (Owner/Admin/Bookkeeper/Approver/
   Read-only/Payroll-only — ``models.role.STARTER_ROLES``) for EVERY
   existing tenant. Idempotent via NOT EXISTS.
4. ENABLE + (deferred) FORCE ROW LEVEL SECURITY with the standard
   symmetric ``tenant_isolation`` policy (same predicate shape as
   0055/0088/0150/0174/0178/0182/0189 — FORCE is deferred until AFTER
   the backfill completes, matching the 0058/0186 "data first, FORCE
   second" precedent, so the owner-role backfill INSERT isn't itself
   blocked by a WITH CHECK that has no ``app.current_tenant`` GUC to
   evaluate).
5. Grants DML to ``saebooks_app`` (the runtime NOBYPASSRLS role).

Self-heal note
--------------
This migration seeds every tenant that exists AT MIGRATION TIME. Any
tenant created afterwards (signup, ephemeral demo, principal-minted)
gets its starter roles from ``services.roles.ensure_starter_roles``,
called defensively at the top of
``services.permissions.resolve_permissions`` on every permission
resolution — so a tenant-creation code path that forgets to seed
roles explicitly can never lock its users out (see
``services/roles.py`` docstring). This migration is the bulk backfill,
not the only seeding path.

Reversibility
-------------
``downgrade()`` drops the policy, lifts FORCE, disables RLS, and drops
the table (backfilled rows are discarded — there is nothing to
reverse, the migration only inserts, never mutates prior data).

Revision ID: 0190_roles_table
Revises:     0189_scheduled_backups
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0190_roles_table"
down_revision: str | None = "0189_scheduled_backups"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "roles"

_BASE_ROLE_VALUES = "'owner', 'admin', 'accountant', 'bookkeeper', 'viewer'"

_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

# (name, base_role) — kept in lockstep with models.role.STARTER_ROLES.
_STARTER_ROLES: tuple[tuple[str, str | None], ...] = (
    ("Owner", "owner"),
    ("Admin", "admin"),
    ("Bookkeeper", "bookkeeper"),
    ("Approver", "accountant"),
    ("Read-only", "viewer"),
    ("Payroll-only", None),
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("base_role", sa.String(16), nullable=True),
        sa.Column(
            "is_system", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"base_role IS NULL OR base_role IN ({_BASE_ROLE_VALUES})",
            name="ck_roles_base_role",
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(
        f"uq_{_TABLE}_tenant_name", _TABLE, ["tenant_id", "name"], unique=True
    )
    op.create_index(
        f"uq_{_TABLE}_tenant_base_role",
        _TABLE,
        ["tenant_id", "base_role"],
        unique=True,
        postgresql_where=sa.text("base_role IS NOT NULL"),
    )

    # --- Backfill: 6 starter roles for every existing tenant. ----------
    # Idempotent (NOT EXISTS) in case this migration is partially re-run.
    conn = op.get_bind()
    tenant_ids = [
        row[0] for row in conn.execute(sa.text("SELECT id FROM tenants"))
    ]
    for tenant_id in tenant_ids:
        for name, base_role in _STARTER_ROLES:
            conn.execute(
                sa.text(
                    f"""
                    INSERT INTO {_TABLE} (tenant_id, name, base_role, is_system)
                    SELECT :tid, :name, :base_role, true
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {_TABLE}
                        WHERE tenant_id = :tid AND name = :name
                    )
                    """
                ).bindparams(tid=tenant_id, name=name, base_role=base_role)
            )

    # --- RLS: ENABLE now, FORCE deferred until after backfill. ---------
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(f"uq_{_TABLE}_tenant_base_role", table_name=_TABLE)
    op.drop_index(f"uq_{_TABLE}_tenant_name", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.drop_table(_TABLE)
