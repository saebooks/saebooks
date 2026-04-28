"""Shared FastAPI dep for the JSON-API ``?hard=true`` admin gate.

The JSON API at ``/api/`` is exempt from the ForwardAuth middleware (see
``OPEN_PATH_PREFIXES`` in ``middleware.auth``), so ``request.state.user``
is None for static-bearer requests. The existing JSON-API admin
convention is the ``X-Admin: true`` request header (see
``users.py:_require_admin``); we mirror that.

When ``request.state.user`` IS populated (forward-auth from saebooks-web,
JWT with sub claim resolving to a User row), we additionally require the
user's role to be ADMIN — so a bookkeeper-JWT request with ``X-Admin:
true`` still 403s.

The gate is a no-op when ``hard`` is absent or false — the route falls
through to its normal soft-delete path.
"""
from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, Query, Request

from saebooks.models.user import UserRole, has_at_least


async def hard_delete_admin_gate(
    request: Request,
    hard: bool = Query(default=False),
    x_admin: str | None = Header(default=None, alias="X-Admin"),
) -> bool:
    """Return ``hard`` after enforcing admin gate when ``hard`` is true.

    403 when ``hard=true`` and neither the X-Admin header is present nor
    the request-state user is an admin. Always returns the (possibly
    False) ``hard`` flag so the route handler can branch on it.
    """
    if not hard:
        return False
    user: Any = getattr(request.state, "user", None)
    if user is not None:
        role: str = getattr(request.state, "role", None) or user.role
        if not has_at_least(role, UserRole.ADMIN.value):
            raise HTTPException(403, "Admin role required for hard-delete")
        return True
    if x_admin is None or x_admin.lower() != "true":
        raise HTTPException(403, "Admin privileges required for hard-delete")
    return True
