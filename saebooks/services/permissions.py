"""Permission resolution.

The permission matrix is (role grants) UNION (user grants) MINUS (user revokes).

* **Role grants** come from ``role_permissions``, keyed on a tenant-
  scoped ``models.role.Role`` row (granular_permissions module, D2) —
  see ``_resolve_role_id`` for how a user's row is found.
* **User grants** come from ``user_permissions`` with ``granted=True``
  — a user can be granted an extra permission without changing their
  role.
* **User revokes** come from ``user_permissions`` with
  ``granted=False`` — a specific user can be denied a permission even
  though their role would otherwise permit it.

``resolve_permissions(session, user)`` returns the final frozenset of
codes. Callers typically hit it once per request and stash the result
on ``request.state.permissions``.

``has_permission(permissions, code)`` is a pure set-membership check.

The permission catalogue itself lives in the ``permissions`` table,
seeded by migration ``0033_permissions`` + extended by
``0192_permission_catalogue_extend``. ``all_permission_codes()``
returns the full catalogue for UI purposes (admin matrix page).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.permission import (
    Permission,
    RolePermission,
    UserPermission,
)
from saebooks.models.role import Role
from saebooks.models.user import User


async def _resolve_role_id(
    session: AsyncSession, user: User
) -> uuid.UUID | None:
    """Find the ``Role`` row that governs ``user``'s fine-grained grants.

    1. ``user.role_id`` if explicitly set — validated to belong to the
       user's own tenant (defence-in-depth; a cross-tenant role_id
       should never be reachable given the write-time check in
       ``api/v1/users.py``, but a stale/foreign id here fails closed
       rather than silently resolving another tenant's role).
    2. Otherwise, the tenant's starter role whose ``base_role`` matches
       ``user.role`` (the legacy rank string — see ``models/role.py``'s
       ``STARTER_ROLES`` mapping).

    Returns ``None`` if neither resolves to a real row (should not
    happen once ``ensure_starter_roles`` has run for the tenant, but
    fails closed — an empty grant set — rather than raising, matching
    ``resolve_permissions``'s "no permission set = every gate 403s"
    default-deny posture).
    """
    if user.role_id is not None:
        role = await session.get(Role, user.role_id)
        if role is not None and role.tenant_id == user.tenant_id:
            return role.id
        # Stale/foreign role_id — fall through to the legacy mapping
        # rather than granting nothing at all; a dangling FK reference
        # (SET NULL on delete should prevent this, but defence-in-depth).

    result = await session.execute(
        select(Role.id).where(
            Role.tenant_id == user.tenant_id,
            Role.base_role == user.role,
            Role.is_system.is_(True),
        )
    )
    return result.scalars().first()


async def resolve_permissions(
    session: AsyncSession, user: User
) -> frozenset[str]:
    """Compute the final permission set for ``user``.

    Archived users return the empty set — every gate 403s.

    Self-heals the tenant's starter roles + grants on every call
    (``ensure_starter_roles`` — cheap once seeded, one indexed SELECT)
    so a tenant-creation code path that forgot to seed roles can never
    lock its own users out. See ``services/roles.py`` module docstring.
    """
    if user.archived_at is not None:
        return frozenset()

    from saebooks.services.roles import ensure_starter_roles

    await ensure_starter_roles(session, user.tenant_id)

    role_id = await _resolve_role_id(session, user)
    granted: set[str] = set()
    if role_id is not None:
        role_rows = (
            await session.execute(
                select(RolePermission.permission_code).where(
                    RolePermission.role_id == role_id,
                    RolePermission.tenant_id == user.tenant_id,
                )
            )
        ).scalars().all()
        granted = set(role_rows)

    # User overrides — filtered by tenant_id as defence-in-depth on top
    # of the user_id scoping (RLS checklist item 6).
    user_rows = (
        await session.execute(
            select(
                UserPermission.permission_code,
                UserPermission.granted,
            ).where(
                UserPermission.user_id == user.id,
                UserPermission.tenant_id == user.tenant_id,
            )
        )
    ).all()
    for code, is_grant in user_rows:
        if is_grant:
            granted.add(code)
        else:
            granted.discard(code)

    return frozenset(granted)


def has_permission(
    permissions: Iterable[str] | None, code: str
) -> bool:
    """Pure check: is ``code`` in ``permissions``?

    ``None`` is safe — returns False. Useful for unauthenticated paths
    where request.state.permissions was never populated.
    """
    if permissions is None:
        return False
    return code in set(permissions)


async def all_permission_codes(session: AsyncSession) -> list[str]:
    """Return the full catalogue, sorted alphabetically."""
    rows = (
        await session.execute(
            select(Permission.code).order_by(Permission.code)
        )
    ).scalars().all()
    return list(rows)


async def all_permissions(
    session: AsyncSession,
) -> list[tuple[str, str]]:
    """Return ``(code, description)`` pairs for the full catalogue."""
    rows = (
        await session.execute(
            select(Permission.code, Permission.description)
            .order_by(Permission.code)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


async def role_grants(
    session: AsyncSession, role_id: uuid.UUID
) -> frozenset[str]:
    """Return the permission codes granted to ``role_id`` (no user override).

    ``role_id`` replaces the pre-D2 bare role STRING parameter — see
    ``services/roles.py`` for the tenant-scoped custom-role CRUD
    (including ``set_role_grants``, which moved there since it now
    needs a ``tenant_id`` to stamp on each inserted row).
    """
    rows = (
        await session.execute(
            select(RolePermission.permission_code).where(
                RolePermission.role_id == role_id
            )
        )
    ).scalars().all()
    return frozenset(rows)


async def grant_user_permission(
    session: AsyncSession,
    user_id: str | uuid.UUID,
    code: str,
    *,
    granted: bool,
    tenant_id: str | uuid.UUID,
    granted_by: str | None = None,
) -> None:
    """Upsert a per-user grant or revoke.

    ``tenant_id`` is required (RLS checklist item 7 — "always set on
    writes, never let it fall back to a default"; see migration
    ``0191_user_permission_tenant_rls``). Callers pass the ACTING
    request's tenant, not a value read off the target user — a
    mismatch between the two would mean the caller is trying to grant
    a permission on a user outside their own tenant, which the FK +
    the caller's own ``svc.get(..., tenant_id=...)`` lookup should
    already have rejected before this is ever called.
    """
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
    tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(tenant_id)

    existing = await session.get(UserPermission, (uid, code))
    if existing is None:
        session.add(
            UserPermission(
                user_id=uid,
                tenant_id=tid,
                permission_code=code,
                granted=granted,
                granted_by=granted_by,
            )
        )
    else:
        existing.granted = granted
        existing.granted_by = granted_by
    await session.commit()


async def revoke_user_override(
    session: AsyncSession, user_id: str | uuid.UUID, code: str
) -> None:
    """Remove the per-user grant/revoke for ``code`` — fall back to role."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
    await session.execute(
        delete(UserPermission).where(
            UserPermission.user_id == uid,
            UserPermission.permission_code == code,
        )
    )
    await session.commit()


__all__ = [
    "all_permission_codes",
    "all_permissions",
    "grant_user_permission",
    "has_permission",
    "resolve_permissions",
    "revoke_user_override",
    "role_grants",
]
