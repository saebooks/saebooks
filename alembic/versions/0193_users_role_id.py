"""users.role_id — explicit custom-role assignment (granular_permissions, D2).

Adds a nullable ``role_id UUID`` FK to ``roles(id)`` on ``users``.
NULL (the default — every existing row stays NULL, no backfill
needed) means "resolve fine-grained permissions via the tenant's
``roles`` row whose ``base_role`` matches this user's legacy
``role`` string" (see ``services/permissions.py``). A non-NULL value
is an explicit assignment to a specific role and is the ONLY way to
reach a role with no legacy equivalent (Payroll-only, or any
genuinely custom role created once entitled to
``FLAG_GRANULAR_PERMISSIONS``).

``users.role`` (the rank-based 5-value string) is completely
UNCHANGED by this migration and keeps driving ``require_role()``'s
coarse gate everywhere it's used today — this column only ever
affects fine-grained resolution (``resolve_permissions`` /
``require_permission``).

No RLS work needed here — ``users`` already carries FORCE RLS +
``tenant_isolation`` from migration 0055; this is a plain additive
column + FK on an already-scoped table.

Additive, reversible: ``downgrade()`` drops the FK + column, discarding
any custom-role assignment (a user with role_id set reverts to legacy-
string-only resolution, never a functional break — see
``services/permissions.py``'s NULL-role_id fallback path).

Revision ID: 0193_users_role_id
Revises:     0192_permission_catalogue_extend
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0193_users_role_id"
down_revision: str | None = "0192_permission_catalogue_extend"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "users_role_id_fkey",
        "users",
        "roles",
        ["role_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_users_role_id", "users", ["role_id"])


def downgrade() -> None:
    op.drop_index("ix_users_role_id", table_name="users")
    op.drop_constraint("users_role_id_fkey", "users", type_="foreignkey")
    op.drop_column("users", "role_id")
