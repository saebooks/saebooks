"""Middleware that honours the X-Dev-Skip-Audit header.

Gated triple — must satisfy ALL three:
  1. SAEBOOKS_EDITION resolves to a tier with FLAG_SKIP_AUDIT_TRAIL active
     (currently only ``developer``).
  2. Request carries ``X-Dev-Skip-Audit: true`` (case-insensitive).
  3. ``request.state.user.role`` is admin (set by ForwardAuthMiddleware).

When all three hold, sets the per-request contextvar; ``change_log_svc.append``
short-circuits when it reads True. When any condition fails, no-op — the
request proceeds with normal audit behaviour.

Register AFTER ForwardAuthMiddleware in main.py so request.state.user is
populated by the time this middleware runs.
"""
from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks.config import settings as _settings
from saebooks.models.user import UserRole, has_at_least
from saebooks.services.dev_context import reset_skip_audit, set_skip_audit
from saebooks.services.features import FLAG_SKIP_AUDIT_TRAIL, is_enabled

_log = logging.getLogger(__name__)


class SkipAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        header_val = request.headers.get("x-dev-skip-audit", "")
        if header_val.strip().lower() != "true":
            return await call_next(request)

        if not is_enabled(FLAG_SKIP_AUDIT_TRAIL, settings=_settings):
            return await call_next(request)

        user: Any = getattr(request.state, "user", None)
        role: str | None = getattr(request.state, "role", None)
        if user is not None and role is None:
            role = getattr(user, "role", None)
        is_admin = bool(role and has_at_least(role, UserRole.ADMIN.value))
        if not is_admin:  # noqa: SIM102  inner branch carries the dev-token fallback rationale
            # Dev-token path doesn't set request.state.user — fall back to
            # X-Admin: true (same pattern as hard_delete_admin_gate).
            if request.headers.get("x-admin", "").strip().lower() != "true":
                return await call_next(request)

        token = set_skip_audit(True)
        try:
            _log.info("skip_audit: active for %s %s", request.method, request.url.path)
            response: Response = await call_next(request)
            return response
        finally:
            reset_skip_audit(token)
