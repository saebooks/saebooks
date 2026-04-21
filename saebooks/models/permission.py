"""Permission catalogue + role/user grant tables (Batch OO).

Three-row ORM for the granular permission matrix:

* ``Permission`` — one row per capability code (e.g. ``invoice.post``).
  Seeded by migration ``0033_permissions``; the code is the primary
  key so grant rows reference it by slug.
* ``RolePermission`` — M2M between a role string (``admin``,
  ``accountant``, etc.) and a permission code. Default grants seeded
  by the migration; admins can add/remove via ``/admin/roles``.
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
    """M2M between role strings and permission codes."""

    __tablename__ = "role_permissions"

    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    permission_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("permissions.code", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserPermission(Base):
    """Per-user grant/revoke — overrides the role table."""

    __tablename__ = "user_permissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
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
