"""Permission catalogue + role/user grant tables.

Three-row ORM for the granular permission matrix:

* ``Permission`` — one row per capability code (e.g. ``invoice.post``).
  Seeded by migration ``0033_permissions``; the code is the primary
  key so grant rows reference it by slug.
* ``RolePermission`` — M2M between a tenant-scoped ``models.role.Role``
  row and a permission code. Prior to migration
  ``0194_role_permissions_rls`` this keyed off a bare GLOBAL
  role string shared by every tenant (the granular_permissions module's
  D2 fix — see that migration's docstring and ``models/role.py``).
  Default grants seeded per-tenant by the migration; admins can
  add/remove via ``POST/PATCH /api/v1/roles`` once entitled to
  ``FLAG_GRANULAR_PERMISSIONS``.
* ``UserPermission`` — per-user override. ``granted=True`` is a grant
  for a user whose role doesn't normally have it; ``granted=False``
  revokes a permission the role would otherwise grant.

The resolver in ``services/permissions.py`` composes all three:
(role grants UNION user grants) MINUS user revokes.

See ``saebooks/services/authz.py:require_permission`` for the FastAPI
dep that uses this.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class Permission(Base):
    """A single capability code with its human-readable description."""

    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RolePermission(Base):
    """M2M between a tenant-scoped ``Role`` row and a permission code.

    ``tenant_id`` is denormalised from the owning ``Role`` (same
    pattern as ``UserPermission`` — avoids a join for the FORCE-RLS
    ``tenant_isolation`` policy to evaluate). Always stamp both
    together; ``services/roles.py`` is the only writer.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    permission_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("permissions.code", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserPermission(Base):
    """Per-user grant/revoke — overrides the role table.

    ``tenant_id`` added by migration ``0191_user_permission_tenant_rls``
    (RLS checklist fix — this table had NO tenant scoping at all before
    that migration; see its docstring). Denormalised from the owning
    user at write time (same pattern 0055/0186 use elsewhere), not
    joined at read time, so the ``tenant_isolation`` FORCE-RLS policy
    doesn't need an expensive join to evaluate.
    """

    __tablename__ = "user_permissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    permission_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("permissions.code", ondelete="CASCADE"),
        primary_key=True,
    )
    granted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    granted_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
