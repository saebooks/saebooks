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

The bottom section adds API-facing helpers (version-aware, change_log
wiring) following the same pattern as ``services/tax_codes.py``. The
original seat-counting functions above remain untouched.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.user import User, UserRole
from saebooks.services import change_log as change_log_svc


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


# ---------------------------------------------------------------------------
# API-oriented helpers (version-aware, change_log wiring)
# The seat-counting functions above remain untouched.
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value.

    The API layer catches this and returns 409 with current server state.
    """

    def __init__(self, current: User) -> None:
        super().__init__(
            f"User {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


# Columns serialised into change_log.payload (password fields excluded)
_USER_COLUMNS: tuple[str, ...] = (
    "id",
    "username",
    "display_name",
    "email",
    "role",
    "last_seen_at",
    "preferred_theme",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise(user: User) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload. Never exposes password."""
    data: dict[str, Any] = {}
    for key in _USER_COLUMNS:
        val = getattr(user, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


async def list_active(
    session: AsyncSession,
    *,
    limit: int = 200,
    offset: int = 0,
    role: str | None = None,
) -> tuple[list[User], int]:
    """Return (page, total) for active (non-archived) users."""
    count_stmt = (
        select(func.count())
        .select_from(User)
        .where(User.archived_at.is_(None))
    )
    if role is not None:
        count_stmt = count_stmt.where(User.role == role)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(User)
        .where(User.archived_at.is_(None))
        .order_by(User.username)
        .offset(offset)
        .limit(limit)
    )
    if role is not None:
        stmt = stmt.where(User.role == role)
    items = list((await session.execute(stmt)).scalars().all())
    return items, int(total)


async def get(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(
        select(User).where(User.username == username)
    )
    return result.scalars().first()


async def create_for_api(
    session: AsyncSession,
    *,
    username: str,
    display_name: str | None = None,
    email: str | None = None,
    role: str = UserRole.READONLY.value,
    preferred_theme: str | None = None,
    actor: str = "api",
) -> User:
    """Create a new user and append a change_log row."""
    user = User(
        username=username.strip(),
        display_name=display_name,
        email=email,
        role=role,
        preferred_theme=preferred_theme,
        version=1,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    await change_log_svc.append(
        session,
        entity="user",
        entity_id=user.id,
        op="create",
        actor=actor,
        payload=_serialise(user),
        version=user.version,
    )
    await session.commit()
    return user


async def update_with_version(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    display_name: str | None = None,
    email: str | None = None,
    role: str | None = None,
    preferred_theme: str | None = None,
    expected_version: int | None = None,
    actor: str | None = None,
    **_ignored: Any,
) -> User:
    """Update a user with optimistic locking + change_log."""
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    if expected_version is not None and user.version != expected_version:
        raise VersionConflict(user)

    if display_name is not None:
        user.display_name = display_name or None
    if email is not None:
        user.email = email or None
    if role is not None:
        user.role = role
    if preferred_theme is not None:
        user.preferred_theme = preferred_theme or None

    user.version = user.version + 1
    user.updated_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(user)
    await change_log_svc.append(
        session,
        entity="user",
        entity_id=user.id,
        op="update",
        actor=actor or "api",
        payload=_serialise(user),
        version=user.version,
    )
    await session.commit()
    return user


async def archive_with_version(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    expected_version: int | None = None,
    actor: str | None = None,
) -> User | None:
    """Soft-archive a user with optimistic locking + change_log."""
    user = await session.get(User, user_id)
    if user is None:
        return None
    if expected_version is not None and user.version != expected_version:
        raise VersionConflict(user)
    user.archived_at = datetime.now(UTC)
    user.version = user.version + 1
    await session.flush()
    await session.refresh(user)
    await change_log_svc.append(
        session,
        entity="user",
        entity_id=user.id,
        op="archive",
        actor=actor or "api",
        payload=_serialise(user),
        version=user.version,
    )
    await session.commit()
    return user
