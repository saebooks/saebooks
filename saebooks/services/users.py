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
from saebooks.services.theme import is_valid_theme_id


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
    "role_id",
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
    tenant_id: uuid.UUID | None = None,
) -> tuple[list[User], int]:
    """Return (page, total) for active (non-archived) users.

    When ``tenant_id`` is supplied queries are filtered to that tenant.
    Keyword-only + optional so existing callers keep working unchanged.
    """
    count_stmt = (
        select(func.count())
        .select_from(User)
        .where(User.archived_at.is_(None))
    )
    if tenant_id is not None:
        count_stmt = count_stmt.where(User.tenant_id == tenant_id)
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
    if tenant_id is not None:
        stmt = stmt.where(User.tenant_id == tenant_id)
    if role is not None:
        stmt = stmt.where(User.role == role)
    items = list((await session.execute(stmt)).scalars().all())
    return items, int(total)


async def get(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> User | None:
    """Fetch a user by id.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    Keyword-only + optional so existing callers keep working unchanged.
    """
    if tenant_id is None:
        return await session.get(User, user_id)
    result = await session.execute(
        select(User).where(
            User.id == user_id,
            User.tenant_id == tenant_id,
        )
    )
    return result.scalars().first()


async def get_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(
        select(User).where(User.username == username)
    )
    return result.scalars().first()


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create_for_api(
    session: AsyncSession,
    *,
    username: str,
    display_name: str | None = None,
    email: str | None = None,
    role: str = UserRole.VIEWER.value,
    role_id: uuid.UUID | None = None,
    preferred_theme: str | None = None,
    actor: str = "api",
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> User:
    """Create a new user and append a change_log row.

    ``preferred_theme`` is validated against ``services.theme.
    ACTIVE_THEMES`` here (write-time, per the intent documented in
    ``alembic/versions/0029_user_preferred_theme.py``) — this is the
    engine's single write path for a new user, so any caller (API
    router, CLI, script) gets the same guard. The API router additionally
    checks this BEFORE calling in, so a bad value there 422s with the
    right HTTP shape; this raise is the defence-in-depth backstop for
    every other caller.

    ``role_id`` — explicit custom-role assignment (granular_permissions,
    D2). The API router is responsible for the FLAG_GRANULAR_PERMISSIONS
    tier gate + the tenant-ownership check on the target role (see
    ``api/v1/users.py``); this layer trusts the caller and only
    persists the value — the FK itself still rejects a genuinely
    nonexistent role id.
    """
    if preferred_theme and not is_valid_theme_id(preferred_theme):
        raise ValueError(f"Unknown theme id: {preferred_theme!r}")
    user = User(
        tenant_id=tenant_id,
        username=username.strip(),
        display_name=display_name,
        email=email,
        role=role,
        role_id=role_id,
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
    role_id: uuid.UUID | None = None,
    preferred_theme: str | None = None,
    expected_version: int | None = None,
    actor: str | None = None,
    **_ignored: Any,
) -> User:
    """Update a user with optimistic locking + change_log.

    ``role_id`` follows the same "None means don't touch" convention
    as ``role``/``email`` above — there is no explicit-clear sentinel
    on this call today. See ``create_for_api``'s docstring for the
    tier-gate/ownership-check division of responsibility.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    if expected_version is not None and user.version != expected_version:
        raise VersionConflict(user)

    if preferred_theme and not is_valid_theme_id(preferred_theme):
        raise ValueError(f"Unknown theme id: {preferred_theme!r}")

    if display_name is not None:
        user.display_name = display_name or None
    if email is not None:
        user.email = email or None
    if role is not None:
        user.role = role
    if role_id is not None:
        user.role_id = role_id
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
