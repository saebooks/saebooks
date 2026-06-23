"""Bump an ephemeral demo's ``last_seen_at`` on each authenticated request.

When ephemeral demos are enabled, this middleware looks at the request's JWT
tenant claim and, if that tenant owns a live demo company, fire-and-forgets a
``last_seen_at = now()`` / ``request_count += 1`` bump so the reaper measures
*real* idle time (the demo only ages out 30m after the last interaction, not
30m after provisioning).

Design notes
------------
* No-op fast path: if ``demo_ephemeral_enabled`` is false, the middleware
  returns immediately — zero cost on non-preview deployments.
* The bump is dispatched as a background task AFTER the response is produced,
  so it never adds latency to the request and never breaks it on a DB hiccup.
* It runs on EVERY request flavour (HTML + JSON) because both the ForwardAuth
  (HTML) and require_bearer (JSON) paths stamp ``request.state.jwt_claims``
  with the tenant before the response. We read that claim; we do not decode the
  JWT ourselves.
* Registered OUTERMOST in the stack (added last) so it observes the claims the
  inner auth middleware stamped.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("saebooks.middleware.demo_touch")


class DemoTouchMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        # Import settings lazily so the env-driven flag is read live (tests
        # flip it per-case) and so module import stays cheap.
        from saebooks.config import settings

        if not settings.demo_ephemeral_enabled:
            return response

        claims = getattr(request.state, "jwt_claims", None)
        tenant_raw = claims.get("tenant_id") if claims else None
        if not tenant_raw:
            return response
        try:
            tenant_id = uuid.UUID(str(tenant_raw))
        except (ValueError, TypeError):
            return response

        # Fire-and-forget — touch_by_tenant is a no-op for non-demo tenants, so
        # the only cost on a normal tenant is one cheap UPDATE that matches no
        # rows. We do not await it; the request has already been served.
        async def _bump() -> None:
            from saebooks.services import ephemeral_demo

            await ephemeral_demo.touch_by_tenant(tenant_id)

        try:
            asyncio.create_task(_bump())
        except RuntimeError:  # pragma: no cover — no running loop (sync test)
            pass
        return response
