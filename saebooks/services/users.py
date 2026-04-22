"""User-level seat helpers for licence enforcement.

CHARTER §6.7 ties seat class to user role:

* **Admin seat**    — user with ``role == UserRole.ADMIN``.
* **Employee seat** — user with any non-admin role (accountant,
                       bookkeeper, readonly, client).
* Archived users don't count against either cap.

These helpers are the data layer for the cap-check predicates in
``services.licence.enforcement``. Routers combine the two:

1. Read current counts with ``count_admin_seats`` + ``count_employee_seats``.
2. Hand the count to the predicate alongside the active edition.
3. Act on the ``CapCheck`` result (block / warn / allow).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.user import User, UserRole


async def count_admin_seats(session: AsyncSession) -> int:
    """Non-archived users whose role is ``admin``."""
    stmt = select(func.count(User.id)).where(
        User.role == UserRole.ADMIN.value,
        User.archived_at.is_(None),
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_employee_seats(session: AsyncSession) -> int:
    """Non-archived users whose role is *not* admin."""
    stmt = select(func.count(User.id)).where(
        User.role != UserRole.ADMIN.value,
        User.archived_at.is_(None),
    )
    return int((await session.execute(stmt)).scalar_one())


def seat_class_for(role: str) -> str:
    """Return ``"admin"`` or ``"employee"`` for a role string.

    Unknown roles are treated as employee — the most conservative
    bucket, because they rank below admin in ``_ROLE_RANK`` and
    shouldn't consume an admin seat.
    """
    return "admin" if role == UserRole.ADMIN.value else "employee"
