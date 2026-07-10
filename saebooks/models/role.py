"""Tenant-scoped roles (granular_permissions module, Richard's D2).

Prior to this module, ``role_permissions.role`` was a bare 5-value
CHECK-constrained string (``owner, admin, accountant, bookkeeper,
viewer``) shared GLOBALLY across every tenant on the instance — no
tenant could rename a role or add a 6th without a schema migration,
and (worse, once role customisation was ever wired) the first tenant
to edit "bookkeeper" would have silently changed it for every other
tenant (see ``permission-matrix-draft.md``'s "Schema gaps" §2).

``Role`` fixes that: one row per (tenant, role). Every tenant gets its
own copy of the six starter roles (Owner, Admin, Bookkeeper, Approver,
Read-only, Payroll-only — seeded by migration ``0190_roles_table`` for
existing tenants, self-healed for any tenant that predates it via
``services.roles.ensure_starter_roles``), and can rename any of them
or add genuinely custom roles once entitled to
``FLAG_GRANULAR_PERMISSIONS`` (see ``services/roles.py`` +
``api/v1/roles.py``).

``base_role`` — the bridge back to the legacy rank-based system
--------------------------------------------------------------------
``users.role`` (the ``UserRole`` 5-value enum: owner/admin/accountant/
bookkeeper/viewer) still drives ``require_role()``'s coarse rank
gate — that mechanism is UNCHANGED by this module and stays the
below-tier / fallback gate everywhere ``require_permission_or_role``
is wired (see ``services/authz.py``). ``base_role`` records, for the
five starter roles that have a legacy-enum equivalent, WHICH legacy
string maps to this row, so ``services.permissions.resolve_permissions``
can find "this tenant's row for a user whose ``users.role`` is X" when
the user has no explicit ``users.role_id`` override. Mapping (matches
the approved draft, "Starter roles mapped to the live 5-value schema
enum"):

* Owner        -> base_role="owner"
* Admin        -> base_role="admin"
* Bookkeeper   -> base_role="bookkeeper"
* Approver     -> base_role="accountant"  (no legacy "approver" value)
* Read-only    -> base_role="viewer"
* Payroll-only -> base_role=NULL          (no legacy equivalent at all —
  only reachable by explicitly setting ``users.role_id`` to this row,
  itself gated behind FLAG_GRANULAR_PERMISSIONS — see api/v1/users.py)

A genuinely custom role (created via ``POST /api/v1/roles`` once
entitled) always has ``base_role=NULL`` and ``is_system=False`` — it
is ONLY reachable via ``users.role_id``, never via the legacy string.

``is_system`` marks the six starter rows: renameable (D2 says roles
are "renameable"), but not deletable (``services.roles.delete_role``
refuses to drop a system row — deleting "Owner" out from under every
owner-mapped user on the tenant would be a self-lockout footgun).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base

# Kept in lockstep with saebooks.models.user.VALID_ROLES — the five
# legacy rank-based values a starter role's base_role may point at.
# NULL (no CHECK match required) is always allowed — see class docstring.
BASE_ROLE_VALUES: frozenset[str] = frozenset(
    {"owner", "admin", "accountant", "bookkeeper", "viewer"}
)


class Role(Base):
    """One tenant-scoped, renameable role."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL for Payroll-only + every genuinely custom role — see docstring.
    base_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# The six starter roles seeded per-tenant by migration 0190 (existing
# tenants) and services.roles.ensure_starter_roles (self-heal / new
# tenants). (name, base_role) — order matters for nothing, but kept in
# the same order as the approved draft for readability.
STARTER_ROLES: tuple[tuple[str, str | None], ...] = (
    ("Owner", "owner"),
    ("Admin", "admin"),
    ("Bookkeeper", "bookkeeper"),
    ("Approver", "accountant"),
    ("Read-only", "viewer"),
    ("Payroll-only", None),
)

__all__ = ["BASE_ROLE_VALUES", "STARTER_ROLES", "Role"]
