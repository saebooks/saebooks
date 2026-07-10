"""user_permissions — tenant_id + FORCE RLS remediation.

Background
----------
``permission-matrix-draft.md``'s "Schema gaps" §1 (confirmed live):
``user_permissions`` (per-user permission grant/revoke overrides,
migration ``0033_permissions``) has never carried a ``tenant_id``
column and was never brought under the ``0055`` P0 cross-tenant-leak
fix — it simply post-dates 0055 and nothing revisited it since. Today
there is zero DB-level backstop: a service-layer bug that queried this
table without filtering by the caller's tenant's users would leak a
per-user grant/revoke cross-tenant, the exact bug class 0055 was
written to close everywhere else. This migration closes it before
``require_permission()`` gets wired to any router (this module lands
enforcement in a later commit in the same branch — never one without
the other, same discipline as 0186's audit_snapshots note).

Tenant-scoping checklist (see feedback_new-table-rls-checklist):
[x] tenant_id NOT NULL column
[x] FK to tenants(id)
[x] ENABLE ROW LEVEL SECURITY + FORCE ROW LEVEL SECURITY
[x] CREATE POLICY tenant_isolation (USING + WITH CHECK)
[x] Index on (tenant_id, ...)
[x] Service-layer filter as defence-in-depth (services/permissions.py
    now takes/filters tenant_id on every grant/revoke/resolve call)
[x] Always-set on writes (services/permissions.py stamps tenant_id
    from the acting user's own tenant; never left to a default)
[x] Cross-tenant probe test added (tests/test_rls_user_permissions.py)
[x] RLS probe test added (same file)

Backfill
--------
100% derivable — every ``user_permissions`` row references a real
``users.id`` via FK, and every ``users`` row has a ``tenant_id``. A
single ``UPDATE ... FROM users`` backfills every row unconditionally
(unlike 0186's audit_snapshots, there is no "genuinely underivable"
category here), so this column goes straight to ``NOT NULL`` in the
same migration — no nullable-then-later-constrain staging needed.

``users`` is FORCE-RLS'd (migration 0055) with no ``app.current_tenant``
GUC available inside a bare alembic migration session, so the backfill
UPDATE's FROM-join against ``users`` needs the same "NO FORCE for the
owner-bypass duration, then FORCE again" bracket 0058 established —
without it the join silently matches zero rows under a non-superuser
migration-runner role.

Revision ID: 0191_user_permission_tenant_rls
Revises:     0190_roles_table
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0191_user_permission_tenant_rls"
down_revision: str | None = "0190_roles_table"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "user_permissions"

_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING


def upgrade() -> None:
    # ---- Step 1: add tenant_id NULLABLE (constrained NOT NULL below,
    # after the backfill — a bare ADD COLUMN ... NOT NULL would fail on
    # any pre-existing row). --------------------------------------------
    op.add_column(
        _TABLE,
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # ---- Step 2: backfill from the owning user's tenant. See module
    # docstring — users is FORCE-RLS'd, so bracket the join in a
    # NO FORCE / FORCE window (0058 precedent) rather than relying on
    # a GUC no bare migration session sets. ------------------------------
    op.execute(sa.text("ALTER TABLE users NO FORCE ROW LEVEL SECURITY"))
    try:
        op.execute(
            sa.text(
                f"""
                UPDATE {_TABLE} up
                SET tenant_id = u.tenant_id
                FROM users u
                WHERE up.user_id = u.id
                  AND up.tenant_id IS NULL
                """
            )
        )
    finally:
        op.execute(sa.text("ALTER TABLE users FORCE ROW LEVEL SECURITY"))

    # ---- Step 3: assert the backfill left nothing NULL, then constrain.
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id IS NULL")
    ).scalar_one()
    if remaining:
        raise AssertionError(
            f"0191: {remaining} user_permissions row(s) left with NULL "
            "tenant_id after backfill — orphaned user_id with no "
            "matching users row? Investigate before re-running."
        )

    op.alter_column(_TABLE, "tenant_id", nullable=False)
    op.create_foreign_key(
        f"{_TABLE}_tenant_id_fkey",
        _TABLE,
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])

    # ---- Step 4: ENABLE + FORCE RLS + standard symmetric tenant_isolation.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.drop_constraint(f"{_TABLE}_tenant_id_fkey", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
