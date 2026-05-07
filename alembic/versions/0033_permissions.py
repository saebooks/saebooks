"""Granular permissions (Batch OO)

Promotes the 3-tier role gate (admin > accountant > bookkeeper >
readonly > client) from Batch EE to a full permission matrix.

Three new tables:

* ``permissions`` — catalogue of ~40 capability codes like
  ``invoice.post`` or ``bas.lodge``. The code is the primary key
  (human-readable slug) so seed data and role-grant tables reference
  it directly without a UUID hop.
* ``role_permissions`` — M2M linking a role string to a permission
  code. Seeded with five default roles mirroring the five
  ``UserRole`` enum values, but the table is a first-class citizen —
  an admin can add/remove grants on the fly via ``/admin/roles``.
* ``user_permissions`` — M2M for per-user overrides. A row here
  grants (``granted=true``) or revokes (``granted=false``) a
  specific permission for one user, overriding the role grant.
  Empty by default — the role grants cover the happy path.

The existing ``users.role`` column stays. The auth flow is now:

1. Middleware stamps ``request.state.role`` from ``users.role``
2. Permission resolution: role grants UNION user grants, minus user
   revokes. Cached per-user per-request in ``request.state.permissions``.
3. ``require_permission("invoice.post")`` raises 403 when the code
   isn't in the resolved set.

Legacy ``require_role`` keeps working unchanged — it checks the role
rank, not the permission set — so existing decorators don't break.
New routes should reach for ``require_permission``.

Additive migration. Running the upgrade on an existing install adds
the three tables + seeds the default grants; no user loses access
because the role-based gates keep firing. Downgrade drops all three
tables cleanly.

Revision ID: 0033_permissions
Revises: 0032_asset_tax_model
Create Date: 2026-04-21
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_permissions"
down_revision: str | None = "0032_asset_tax_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Catalogue seed — kept in the migration so a fresh install has
# something useful to grant. The services/permissions.py module
# imports PERMISSION_SEED from here indirectly (re-declared there)
# so callers get a Python-land reference — don't rely on a DB round
# trip inside authz hot paths.
_PERMISSIONS: list[tuple[str, str]] = [
    # --- View (all roles incl. readonly/client) -------------------- #
    ("dashboard.view", "View the dashboard"),
    ("contact.view", "View contacts"),
    ("account.view", "View chart of accounts"),
    ("invoice.view", "View invoices"),
    ("bill.view", "View bills"),
    ("credit_note.view", "View credit notes"),
    ("payment.view", "View payments"),
    ("journal.view", "View journal entries"),
    ("report.view", "View reports"),
    ("asset.view", "View fixed assets"),
    ("bank.view", "View bank feeds + statement lines"),
    ("item.view", "View inventory items"),
    ("project.view", "View projects"),
    ("budget.view", "View budgets"),
    # --- Create / edit (bookkeeper and up) ------------------------- #
    ("contact.edit", "Create and edit contacts"),
    ("invoice.create", "Create draft invoices"),
    ("bill.create", "Create draft bills"),
    ("payment.create", "Create payments"),
    ("credit_note.create", "Create credit notes"),
    ("journal.draft", "Create draft journal entries"),
    ("item.edit", "Create and edit inventory items"),
    ("project.edit", "Create and edit projects"),
    # --- Post / state change (accountant and up) ------------------- #
    ("invoice.post", "Post invoices to the GL"),
    ("bill.post", "Post bills to the GL"),
    ("payment.post", "Post payments to the GL"),
    ("credit_note.post", "Post credit notes to the GL"),
    ("journal.post", "Post journal entries"),
    ("journal.reverse", "Reverse a posted journal entry"),
    ("asset.edit", "Create, edit, dispose fixed assets"),
    ("asset.depreciate", "Post depreciation journals"),
    ("budget.edit", "Create and edit budgets"),
    ("account.edit", "Create and edit chart-of-accounts rows"),
    ("bank.sync", "Run bank feed sync and reconciliation"),
    # --- Destructive / terminal (accountant + admin) --------------- #
    ("invoice.void", "Void a posted invoice"),
    ("bill.void", "Void a posted bill"),
    ("payment.void", "Void a posted payment"),
    ("period.close", "Post a year-end close and lock the period"),
    ("bas.lodge", "Lodge BAS to the ATO"),
    ("payroll.run", "Run payroll and submit STP"),
    # --- Admin-only ------------------------------------------------ #
    ("user.admin", "Manage user roles and permission overrides"),
    ("company.delete", "Hard-delete a company (offboarding)"),
    ("integration.configure", "Configure SISS, ABR, Paperless, Stripe, LEI"),
    ("audit.export", "Export the audit log as CSV"),
    ("company.export", "Export the full company data bundle"),
    ("settings.edit", "Edit company settings and theme"),
]


# Default role grants — keyed on the legacy UserRole values. Five roles
# x expected capability set. "viewer" is the legacy readonly.
_ROLE_GRANTS: dict[str, list[str]] = {
    "admin": [code for code, _ in _PERMISSIONS],  # admin gets everything
    "accountant": [
        code
        for code, _ in _PERMISSIONS
        # Accountant excludes the admin-only set
        if code
        not in {
            "user.admin",
            "company.delete",
            "integration.configure",
            "settings.edit",
        }
    ],
    "bookkeeper": [
        "dashboard.view",
        "contact.view",
        "contact.edit",
        "account.view",
        "invoice.view",
        "invoice.create",
        "invoice.post",
        "bill.view",
        "bill.create",
        "bill.post",
        "credit_note.view",
        "credit_note.create",
        "payment.view",
        "payment.create",
        "payment.post",
        "journal.view",
        "journal.draft",
        "report.view",
        "asset.view",
        "bank.view",
        "bank.sync",
        "item.view",
        "item.edit",
        "project.view",
        "budget.view",
    ],
    "readonly": [code for code, _ in _PERMISSIONS if code.endswith(".view")],
    "client": [
        "dashboard.view",
        "invoice.view",
        "bill.view",
        "payment.view",
        "report.view",
    ],
}


def upgrade() -> None:
    # --- permissions table + seed -------------------------------------- #
    op.create_table(
        "permissions",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    conn = op.get_bind()
    for code, description in _PERMISSIONS:
        conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description) "
                "VALUES (:code, :description)"
            ),
            {"code": code, "description": description},
        )

    # --- role_permissions M2M + seed ----------------------------------- #
    op.create_table(
        "role_permissions",
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "permission_code",
            sa.String(64),
            sa.ForeignKey("permissions.code", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "role", "permission_code", name="pk_role_permissions"
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'accountant', 'bookkeeper', 'readonly', 'client')",
            name="ck_role_permissions_role",
        ),
    )
    op.create_index(
        "ix_role_permissions_role", "role_permissions", ["role"]
    )

    for role, grants in _ROLE_GRANTS.items():
        for code in grants:
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role, permission_code) "
                    "VALUES (:role, :code)"
                ),
                {"role": role, "code": code},
            )

    # --- user_permissions (per-user override) -------------------------- #
    op.create_table(
        "user_permissions",
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_code",
            sa.String(64),
            sa.ForeignKey("permissions.code", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
            comment="True = grant, False = revoke. Overrides role grants.",
        ),
        sa.Column(
            "granted_by",
            sa.String(64),
            nullable=True,
            comment="Username of the admin who made the grant/revoke",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "permission_code", name="pk_user_permissions"
        ),
    )
    op.create_index(
        "ix_user_permissions_user", "user_permissions", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_permissions_user", table_name="user_permissions")
    op.drop_table("user_permissions")
    op.drop_index("ix_role_permissions_role", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
