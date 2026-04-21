"""Role-based authorisation — FastAPI deps for role gates.

Pairs with ``saebooks.middleware.auth`` which stamps
``request.state.user`` / ``request.state.role``.

Usage::

    from fastapi import APIRouter, Depends
    from saebooks.services.authz import require_role

    router = APIRouter()

    @router.post(
        "/admin/dangerous",
        dependencies=[Depends(require_role("admin"))],
    )
    async def dangerous() -> ...: ...

``require_role`` is a factory — the returned dep inspects
``request.state.role`` on each request, raising 403 when the role is
absent or below the required rank. 401 is raised when there is no
user at all — that distinguishes "log in please" from "you're logged
in but not allowed".

``current_user`` is the non-gated ready-reference dep for the already-
authenticated request — use it on ``/whoami`` and similar
self-service routes.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User, UserRole, has_at_least
from saebooks.services import permissions as perm_svc


def current_user(request: Request) -> User | None:
    """Return the user attached by ForwardAuthMiddleware, or ``None``."""
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    assert isinstance(user, User)
    return user


def require_user() -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no authenticated user on the request."""
    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        return user

    return _dep


def require_role(
    minimum: str | UserRole,
) -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no user, 403 if the user's role ranks below ``minimum``.

    ``minimum`` can be either the string literal (``"admin"``) or the
    enum member (``UserRole.ADMIN``). Role hierarchy follows
    ``models.user._ROLE_RANK`` — admin > accountant > bookkeeper >
    readonly > client.
    """
    required = minimum.value if isinstance(minimum, UserRole) else str(minimum)

    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        role = getattr(request.state, "role", None) or user.role
        if not has_at_least(role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{required}' required (you have '{role}')",
            )
        return user

    return _dep


async def _resolve_and_cache(request: Request, user: User) -> frozenset[str]:
    """Lazily resolve the request's permission set, cache on state."""
    cached: frozenset[str] | None = getattr(
        request.state, "permissions", None
    )
    if cached is not None:
        return cached
    async with AsyncSessionLocal() as session:
        resolved = await perm_svc.resolve_permissions(session, user)
    request.state.permissions = resolved
    return resolved


def require_permission(
    code: str,
) -> Callable[[Request], Awaitable[User]]:
    """Dep: 401 if no user, 403 if ``code`` isn't in the user's permission set.

    Uses ``services/permissions.resolve_permissions`` which composes
    role grants + per-user overrides. Resolution is cached on
    ``request.state.permissions`` so multiple permission-gated deps
    on the same request only hit the DB once.
    """

    async def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        perms = await _resolve_and_cache(request, user)
        if code not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission {code!r} required",
            )
        return user

    return _dep


async def permissions_for(request: Request) -> frozenset[str]:
    """Reader dep for templates + debug routes — never 401/403.

    Returns an empty frozenset when no user is on the request (anonymous
    / open path). Templates that want to hide a button unless the user
    has a permission should read the value off ``request.state.permissions``
    directly (populated by this dep on first call).
    """
    user = current_user(request)
    if user is None:
        return frozenset()
    return await _resolve_and_cache(request, user)
