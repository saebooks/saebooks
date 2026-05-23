"""Gate for FLAG_EDIT_FROZEN_STATE — admin can edit frozen-state entities.

Mirrors ``hard_delete_admin_gate`` but for PATCH endpoints. Pass ``?force=true``
as a query param. Returns the bool the route handler then forwards to its
service-layer ``force=`` kwarg.

Triple-gated like every dev-mode override:
  1. FLAG_EDIT_FROZEN_STATE active on the instance (developer tier).
  2. ``?force=true`` query param present.
  3. Caller is admin (request.state.role).

Returns ``False`` when ``force`` is absent — route's normal validation
path runs as usual.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query, Request

from saebooks.config import settings as _settings
from saebooks.models.user import UserRole, has_at_least
from saebooks.services.features import FLAG_EDIT_FROZEN_STATE, is_enabled


async def edit_force_admin_gate(
    request: Request,
    force: bool = Query(default=False),
) -> bool:
    """Return ``force`` after enforcing admin gate + flag check when true."""
    if not force:
        return False
    if not is_enabled(FLAG_EDIT_FROZEN_STATE, settings=_settings):
        raise HTTPException(404, "Not found")
    user: Any = getattr(request.state, "user", None)
    role: str | None = getattr(request.state, "role", None)
    if not role and user is not None:
        role = getattr(user, "role", None)
    if not role or not has_at_least(role, UserRole.ADMIN.value):
        raise HTTPException(403, "Admin role required to edit frozen state")
    return True
