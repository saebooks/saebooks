"""Permission resolution (Batch OO).

The permission matrix is (role grants) UNION (user grants) MINUS (user revokes).

* **Role grants** come from ``role_permissions``. The user's ``role``
  column picks which set applies.
* **User grants** come from ``user_permissions`` with ``granted=True``
  — a user with role ``bookkeeper`` can be granted ``bas.lodge``
  without being bumped up to accountant.
* **User revokes** come from ``user_permissions`` with
  ``granted=False`` — a specific user can be denied ``invoice.void``
  even though their role would otherwise permit it.

``resolve_permissions(session, user)`` returns the final frozenset of
codes. Callers typically hit it once per request and stash the result
on ``request.state.permissions``.

``has_permission(permissions, code)`` is a pure set-membership check.

The permission catalogue itself lives in the ``permissions`` table,
seeded by migration ``0033_permissions``. ``all_permission_codes()``
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
from saebooks.models.user import User


async def resolve_permissions(
    session: AsyncSession, user: User
) -> frozenset[str]:
    """Compute the final permission set for ``user``.

    Archived users return the empty set — every gate 403s.
    """
    if user.archived_at is not None:
        return frozenset()

    # Role grants — one round trip
    role_rows = (
        await session.execute(
            select(RolePermission.permission_code).where(
                RolePermission.role == user.role
            )
        )
    ).scalars().all()
    granted: set[str] = set(role_rows)

    # User overrides — second round trip
    user_rows = (
        await session.execute(
            select(
                UserPermission.permission_code,
                UserPermission.granted,
            ).where(UserPermission.user_id == user.id)
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
    session: AsyncSession, role: str
) -> frozenset[str]:
    """Return the permission codes granted to ``role`` (no user override)."""
    rows = (
        await session.execute(
            select(RolePermission.permission_code).where(
                RolePermission.role == role
            )
        )
    ).scalars().all()
    return frozenset(rows)


async def set_role_grants(
    session: AsyncSession,
    role: str,
    codes: Iterable[str],
) -> None:
    """Replace the grants for ``role`` with ``codes``.

    Deletes existing rows + inserts the new set in a single transaction.
    No-ops when ``codes`` produces the same set as already exists.
    """
    existing = await role_grants(session, role)
    target = frozenset(codes)
    if existing == target:
        return

    # Delete-all + insert-all is safest for this kind of full-set swap
    await session.execute(
        delete(RolePermission).where(RolePermission.role == role)
    )
    for code in sorted(target):
        session.add(RolePermission(role=role, permission_code=code))
    await session.commit()


async def grant_user_permission(
    session: AsyncSession,
    user_id: str | uuid.UUID,
    code: str,
    *,
    granted: bool,
    granted_by: str | None = None,
) -> None:
    """Upsert a per-user grant or revoke."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)

    existing = await session.get(UserPermission, (uid, code))
    if existing is None:
        session.add(
            UserPermission(
                user_id=uid,
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
    "set_role_grants",
]
