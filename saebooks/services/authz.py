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

from saebooks.models.user import User, UserRole, has_at_least


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
