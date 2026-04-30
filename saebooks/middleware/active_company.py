"""Stash the active company on ``request.state`` for templates to read.

The web UI's company switcher in ``base.html`` reads
``request.state.active_company`` and ``request.state.companies_for_switcher``.
Without a middleware, every router that renders a template would need
to fetch and pass them by hand — 30+ routers, each with multiple
handlers — which is exactly the maintenance hole the legacy
``_first_company()`` pattern dug.

This middleware does the lookup once per HTML request:

* Skip non-HTML paths (``/static``, ``/api``, ``/healthz``,
  ``/metrics``, ``/webhooks``, ``/favicon.ico``) so the DB isn't hit
  for asset / probe traffic.
* Skip the request entirely if tenant resolution fails — that means
  the request is anonymous (e.g. a probe before auth has run); the
  switcher just won't render. We don't want middleware blowing up
  legitimate health checks.
* Resolve the tenant via the same path API routes use
  (``resolve_tenant_id``) so a request that lacks a JWT in production
  doesn't get silently bound to ``DEFAULT_TENANT_ID`` — the resolver
  will raise 401 in that case, which we catch and treat as "no active
  company to stash".
* Cache the resolved tuple on ``request.state`` and continue.

The middleware does NOT touch the contextvar in
``saebooks.services.tenant``. That contextvar is reserved for the
ORM-listener defence-in-depth; binding it here would change query
behaviour in every code path and would need its own correctness pass.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.db import AsyncSessionLocal
from saebooks.services import active_company as active_svc

_LOG = logging.getLogger("saebooks.active_company_mw")

# Path prefixes that never render base.html — skip the DB lookup.
_SKIP_PREFIXES = (
    "/static/",
    "/api/",
    "/healthz",
    "/metrics",
    "/webhooks/",
    "/favicon.ico",
    "/grpc/",
)


class ActiveCompanyMiddleware(BaseHTTPMiddleware):
    """Stash ``request.state.active_company`` + ``companies_for_switcher``."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if not any(path.startswith(p) for p in _SKIP_PREFIXES):
            try:
                tenant_id = resolve_tenant_id(request)
            except HTTPException:
                # Anonymous / no JWT in prod — no switcher to render.
                tenant_id = None

            if tenant_id is not None:
                try:
                    async with AsyncSessionLocal() as session:
                        active, companies = (
                            await active_svc.resolve_active_with_options(
                                session, request, tenant_id
                            )
                        )
                except HTTPException:
                    # No companies in tenant — let the route handle it.
                    active, companies = None, []
                except Exception as exc:  # pragma: no cover - safety net
                    # Don't take the whole site down because the
                    # switcher couldn't resolve. Log + fall through.
                    _LOG.warning("active company lookup failed: %s", exc)
                    active, companies = None, []
                request.state.active_company = active
                request.state.companies_for_switcher = companies

        return await call_next(request)
