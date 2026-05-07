"""Multi-tenant practitioner — create user_tenant_memberships + backfill.

Rationale and decision log: ``docs/design/multi-tenant-membership/design.md``.

What this migration does
------------------------
1. Drops the legacy ``ck_users_role_valid`` CHECK constraint and replaces it
   with the new role enum (``owner``, ``admin``, ``accountant``,
   ``bookkeeper``, ``viewer``). The legacy ``readonly`` role is renamed to
   ``viewer`` and the ``client`` role is collapsed to ``viewer``.
2. Drops the legacy ``ck_role_permissions_role`` CHECK constraint and
   replaces it with the same set; renames any ``readonly`` /``client`` rows
   to ``viewer``; copies the existing ``admin`` grants under a new ``owner``
   row.
3. Creates the ``user_tenant_memberships`` table with partial unique
   indexes for the (user_id, tenant_id) and (user_id, is_default)
   invariants.
4. Enables FORCE ROW LEVEL SECURITY on the new table with a self-only
   policy + SAE-staff bypass via ``app.is_staff`` GUC.
5. Backfills one membership per active user from ``users.tenant_id``.
6. Asserts the post-migration invariants (every active user has exactly
   one active membership; no duplicates; row count matches).

What this migration does NOT do
-------------------------------
- Drop ``users.tenant_id``. That column is preserved for one release as a
  fallback / "home tenant" pointer. A follow-up migration (0059) will
  drop it after the application code has been updated to read memberships
  exclusively.
- Modify ``app.current_tenant`` / ``tenant_isolation`` policies on any
  existing table. The membership work is read-side metadata. The only
  RLS change is for the new ``user_tenant_memberships`` table itself,
  which is intentionally NOT under ``tenant_isolation`` (it crosses
  tenants).

Reversibility
-------------
``downgrade`` drops the membership table and restores the original
``ck_users_role_valid`` and ``ck_role_permissions_role`` CHECK
constraints. The downgrade tolerates the renamed ``readonly`` rows by
mapping ``viewer`` back to ``readonly`` before re-asserting the legacy
CHECK. ``client`` rows lost during upgrade are NOT restored (they were
collapsed to ``viewer``); the old constraint readmits the value but no
row will hold it post-downgrade. This is acceptable — production has no
``client`` rows today and the test suite does not depend on the role.

Revision ID: 0058_user_tenant_memberships
Revises: 0057_idempotency_storage
Create Date: 2026-04-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision: str = "0058_user_tenant_memberships"
down_revision: str | None = "0057_idempotency_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "user_tenant_memberships"

# The new role enum. Keep this list in lockstep with:
#   - saebooks/models/user.py : UserRole + _ROLE_RANK
#   - schema.sql ck_utm_role_valid CHECK
#   - role-enum.md
_NEW_ROLES: tuple[str, ...] = (
    "owner",
    "admin",
    "accountant",
    "bookkeeper",
    "viewer",
)

# Mapping for the role rename.  client -> viewer is a collapse; readonly ->
# viewer is a straight rename.
_LEGACY_ROLE_MAP: dict[str, str] = {
    "readonly": "viewer",
    "client": "viewer",
}


def _legacy_check_clause() -> str:
    """Original CHECK from migration 0025."""
    return "role IN ('admin', 'accountant', 'bookkeeper', 'readonly', 'client')"


def _new_check_clause() -> str:
    """CHECK after this migration."""
    quoted = ", ".join(f"'{r}'" for r in _NEW_ROLES)
    return f"role IN ({quoted})"


# ---------------------------------------------------------------------------
# Pre/post invariant checks
# ---------------------------------------------------------------------------


def _assert(condition: bool, message: str) -> None:
    """Raise an explicit error if ``condition`` is false.

    Migrations that hit a data-quality issue should fail loud and early so
    the operator sees it before the alembic_version row commits. The
    transaction is automatically rolled back when this raises.
    """
    if not condition:
        raise AssertionError(message)


def _check_pre_invariants() -> None:
    """Sanity-check the source data before we touch anything.

    We expect:
    1. Every active user (archived_at IS NULL) has a non-null tenant_id.
    2. No active user holds a role outside the legacy enum (defensive —
       the existing CHECK should make this trivially true).
    3. Every users.tenant_id references a real tenants.id.
    """
    bind = op.get_bind()

    bad_tenant = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM users "
            "WHERE archived_at IS NULL AND tenant_id IS NULL"
        )
    ).scalar_one()
    _assert(
        bad_tenant == 0,
        f"PRE-CHECK FAILED: {bad_tenant} active users with NULL tenant_id",
    )

    bad_role = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM users "
            "WHERE archived_at IS NULL "
            "AND role NOT IN ('admin','accountant','bookkeeper','readonly','client')"
        )
    ).scalar_one()
    _assert(
        bad_role == 0,
        f"PRE-CHECK FAILED: {bad_role} active users with unknown role string",
    )

    orphan_tenant = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM users u "
            "LEFT JOIN tenants t ON t.id = u.tenant_id "
            "WHERE u.archived_at IS NULL AND t.id IS NULL"
        )
    ).scalar_one()
    _assert(
        orphan_tenant == 0,
        f"PRE-CHECK FAILED: {orphan_tenant} active users reference a non-existent tenant",
    )


def _check_post_invariants() -> None:
    """Verify the membership table reflects the source data exactly.

    Three post-conditions:
    1. Every active user has exactly one active membership.
    2. The (user_id, tenant_id) of each membership matches users.tenant_id.
    3. Exactly one is_default = true row per active user.
    """
    bind = op.get_bind()

    user_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE archived_at IS NULL")
    ).scalar_one()
    membership_count = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE revoked_at IS NULL"  # noqa: S608
        )
    ).scalar_one()
    _assert(
        user_count == membership_count,
        f"POST-CHECK FAILED: {user_count} active users vs "
        f"{membership_count} active memberships",
    )

    mismatch = bind.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM users u
            LEFT JOIN {_TABLE} m
              ON m.user_id = u.id
             AND m.tenant_id = u.tenant_id
             AND m.revoked_at IS NULL
            WHERE u.archived_at IS NULL
              AND m.id IS NULL
            """  # noqa: S608
        )
    ).scalar_one()
    _assert(
        mismatch == 0,
        f"POST-CHECK FAILED: {mismatch} active users without a matching membership",
    )

    multi_default = bind.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM (
                SELECT user_id, COUNT(*) AS c
                FROM {_TABLE}
                WHERE is_default = true AND revoked_at IS NULL
                GROUP BY user_id
                HAVING COUNT(*) > 1
            ) AS bad
            """  # noqa: S608
        )
    ).scalar_one()
    _assert(
        multi_default == 0,
        f"POST-CHECK FAILED: {multi_default} users with more than one default membership",
    )

    no_default = bind.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM users u
            WHERE u.archived_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {_TABLE} m
                  WHERE m.user_id = u.id
                    AND m.is_default = true
                    AND m.revoked_at IS NULL
              )
            """  # noqa: S608
        )
    ).scalar_one()
    _assert(
        no_default == 0,
        f"POST-CHECK FAILED: {no_default} active users with zero default memberships",
    )


# ---------------------------------------------------------------------------
# Role rename helpers — applied to existing tables before the membership
# table is created. Splits out so upgrade() reads top-down.
# ---------------------------------------------------------------------------


def _rename_legacy_roles_in_users() -> None:
    """Rename users.role values in place (readonly -> viewer, client -> viewer).

    Drop the old CHECK first because the new value would otherwise be
    rejected by the legacy constraint mid-update.

    RLS / FORCE handling
    --------------------
    Migration 0055 sets ``FORCE ROW LEVEL SECURITY`` on ``users`` with a
    ``tenant_isolation`` policy keyed on ``app.current_tenant``. Alembic
    does not (and should not) set that GUC, so a naive
    ``UPDATE users SET role = ...`` would match zero rows on a
    non-superuser owner because the policy predicate evaluates NULL.

    Today ``saebooks2`` happens to be a Postgres superuser (from the
    image default) and therefore bypasses RLS regardless of FORCE — but
    the design in 0056 envisions tightening that, and any environment
    that runs migrations as a plain table owner would silently corrupt.
    Defence-in-depth: temporarily lift FORCE for the owner-bypass
    duration of the rename, then restore it. ``NO FORCE`` only affects
    the *table owner*; other roles remain bound by the policy. The whole
    block runs inside the alembic migration transaction so no concurrent
    reader can observe the gap. ALTER TABLE ... [NO] FORCE is fully
    transactional in PostgreSQL (verified empirically against PG 16).
    """
    op.execute(sa.text("ALTER TABLE users DROP CONSTRAINT ck_users_role_valid"))

    op.execute(sa.text("ALTER TABLE users NO FORCE ROW LEVEL SECURITY"))
    try:
        for old, new in _LEGACY_ROLE_MAP.items():
            op.execute(
                sa.text(
                    "UPDATE users SET role = :new WHERE role = :old"
                ).bindparams(old=old, new=new)
            )
    finally:
        op.execute(sa.text("ALTER TABLE users FORCE ROW LEVEL SECURITY"))

    op.execute(
        sa.text(
            f"ALTER TABLE users ADD CONSTRAINT ck_users_role_valid "
            f"CHECK ({_new_check_clause()})"
        )
    )


def _rename_legacy_roles_in_role_permissions() -> None:
    """Rename role_permissions.role values; copy admin grants under owner.

    Order matters here:
    1. Drop the CHECK so we can write the new strings.
    2. Rename readonly -> viewer.
    3. Delete client rows (they collapse to viewer, which already exists
       with a richer grant set; we don't merge — viewer's grant set wins).
    4. Insert (role='owner', permission_code=p) for every (admin, p) so
       owners inherit everything an admin can do, plus billing.manage
       which is reserved for the future ``/admin/billing`` route.
    5. Re-add the CHECK with the new enum.
    """
    op.execute(
        sa.text(
            "ALTER TABLE role_permissions DROP CONSTRAINT ck_role_permissions_role"
        )
    )

    op.execute(
        sa.text(
            "UPDATE role_permissions SET role = 'viewer' WHERE role = 'readonly'"
        )
    )
    op.execute(sa.text("DELETE FROM role_permissions WHERE role = 'client'"))

    # Owner inherits every admin grant. Idempotent via NOT EXISTS in case
    # the migration is partially re-run.
    op.execute(
        sa.text(
            """
            INSERT INTO role_permissions (role, permission_code)
            SELECT 'owner', rp.permission_code
            FROM role_permissions rp
            WHERE rp.role = 'admin'
              AND NOT EXISTS (
                  SELECT 1 FROM role_permissions e
                  WHERE e.role = 'owner' AND e.permission_code = rp.permission_code
              )
            """
        )
    )

    op.execute(
        sa.text(
            f"ALTER TABLE role_permissions ADD CONSTRAINT ck_role_permissions_role "
            f"CHECK ({_new_check_clause()})"
        )
    )


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # --- Pre-flight ---------------------------------------------------------
    _check_pre_invariants()

    # --- Role rename (users + role_permissions) -----------------------------
    _rename_legacy_roles_in_users()
    _rename_legacy_roles_in_role_permissions()

    # --- Create the membership table ----------------------------------------
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "granted_by",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            _new_check_clause(),
            name="ck_utm_role_valid",
        ),
    )

    # --- Partial unique indexes for the invariants --------------------------
    op.create_index(
        "uq_utm_active_user_tenant",
        _TABLE,
        ["user_id", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_utm_default_per_user",
        _TABLE,
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true AND revoked_at IS NULL"),
    )

    # --- Hot-path lookup indexes -------------------------------------------
    op.create_index(
        "ix_utm_user_active",
        _TABLE,
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_utm_tenant_active",
        _TABLE,
        ["tenant_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # --- RLS: self-only with SAE-staff bypass -------------------------------
    # Order matters: ENABLE + CREATE POLICY now, but DEFER ``FORCE ROW
    # LEVEL SECURITY`` until *after* the backfill and post-flight checks
    # complete (see end of upgrade()). Reasons:
    #
    # * The policy predicate references ``app.current_user`` and
    #   ``app.is_staff`` GUCs, neither of which alembic sets. With FORCE
    #   active, the backfill INSERT's WITH CHECK evaluates NULL and
    #   Postgres rejects every row, and the post-check SELECTs return
    #   zero rows on a non-superuser owner.
    # * Without FORCE, the table owner (``saebooks2``) bypasses the
    #   policy during the migration window, the backfill INSERTs cleanly,
    #   and post-checks see the true row count. Other roles still get
    #   the policy from ENABLE.
    # * FORCE flips on at the very end of upgrade() so the runtime
    #   ``saebooks_app`` role (and any future non-superuser owner) is
    #   bound by the policy from then on.
    #
    # This mirrors the ``data first, FORCE second`` precedent set by
    # migrations 0041 and 0055.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS utm_self_only ON {_TABLE}"))
    op.execute(
        sa.text(
            f"""
            CREATE POLICY utm_self_only ON {_TABLE}
              FOR ALL
              USING (
                  user_id = current_setting('app.current_user', true)::uuid
                  OR current_setting('app.is_staff', true) = 'true'
              )
              WITH CHECK (
                  user_id = current_setting('app.current_user', true)::uuid
                  OR current_setting('app.is_staff', true) = 'true'
              )
            """
        )
    )

    # --- Grant DML to the runtime app role ----------------------------------
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE
                      ON TABLE {_TABLE} TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )

    # --- Backfill -----------------------------------------------------------
    # One membership per active user; legacy role mapped through
    # _LEGACY_ROLE_MAP. is_default=true because the existing user has
    # exactly one tenant.  Idempotent via NOT EXISTS in case the migration
    # is partially re-run after a DDL failure earlier in this transaction.
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_TABLE}
                (user_id, tenant_id, role, is_default, granted_by, granted_at, revoked_at)
            SELECT
                u.id,
                u.tenant_id,
                u.role,                  -- already migrated above
                true,
                NULL,
                COALESCE(u.created_at, now()),
                NULL
            FROM users u
            WHERE u.archived_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {_TABLE} m
                  WHERE m.user_id = u.id
                    AND m.tenant_id = u.tenant_id
                    AND m.revoked_at IS NULL
              )
            """  # noqa: S608
        )
    )

    # --- Post-flight --------------------------------------------------------
    _check_post_invariants()

    # --- Now FORCE the new table -------------------------------------------
    # Backfill complete and verified. Flip FORCE so even the table
    # owner (and the runtime ``saebooks_app`` role) is bound by
    # ``utm_self_only``. From here on, any session reading the table
    # must set ``app.current_user`` (or ``app.is_staff = 'true'``).
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # 1. Drop the new table (cascades the policy and indexes).
    # NO FORCE first so the policy can't bite any catalog-cleanup
    # query. DROP TABLE itself is DDL and would succeed regardless,
    # but lifting FORCE explicitly mirrors the upgrade() symmetry and
    # keeps the path identical for non-superuser owners.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS utm_self_only ON {_TABLE}"))
    op.drop_index("ix_utm_tenant_active", table_name=_TABLE)
    op.drop_index("ix_utm_user_active", table_name=_TABLE)
    op.drop_index("uq_utm_default_per_user", table_name=_TABLE)
    op.drop_index("uq_utm_active_user_tenant", table_name=_TABLE)
    op.drop_table(_TABLE)

    # 2. Reverse the role rename on role_permissions.
    op.execute(
        sa.text(
            "ALTER TABLE role_permissions DROP CONSTRAINT ck_role_permissions_role"
        )
    )
    op.execute(sa.text("DELETE FROM role_permissions WHERE role = 'owner'"))
    op.execute(
        sa.text(
            "UPDATE role_permissions SET role = 'readonly' WHERE role = 'viewer'"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE role_permissions ADD CONSTRAINT ck_role_permissions_role "
            f"CHECK ({_legacy_check_clause()})"
        )
    )

    # 3. Reverse the role rename on users.
    op.execute(sa.text("ALTER TABLE users DROP CONSTRAINT ck_users_role_valid"))

    # Same FORCE-RLS lift-and-restore as upgrade(): the UPDATE is
    # row-level DML so the ``tenant_isolation`` policy applies. Lift
    # FORCE for owner-bypass during the rename, then restore.
    op.execute(sa.text("ALTER TABLE users NO FORCE ROW LEVEL SECURITY"))
    try:
        # Best-effort revert: viewer -> readonly. ``client`` is not
        # restored (no row currently holds it; see migration docstring).
        op.execute(
            sa.text("UPDATE users SET role = 'readonly' WHERE role = 'viewer'")
        )
        # If any owner rows linger (someone manually set role='owner'
        # between upgrade and downgrade), demote to admin so the legacy
        # CHECK admits the row. This is the safest reversible choice —
        # owners had at least admin authority by definition.
        op.execute(
            sa.text("UPDATE users SET role = 'admin' WHERE role = 'owner'")
        )
    finally:
        op.execute(sa.text("ALTER TABLE users FORCE ROW LEVEL SECURITY"))

    op.execute(
        sa.text(
            f"ALTER TABLE users ADD CONSTRAINT ck_users_role_valid "
            f"CHECK ({_legacy_check_clause()})"
        )
    )
