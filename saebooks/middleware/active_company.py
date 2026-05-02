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
* Resolve the tenant: prefer ``resolve_tenant_id`` (in case an
  earlier middleware already stamped ``request.state.jwt_claims``),
  otherwise decode the bearer JWT inline. ``require_bearer`` runs as
  a Depends, NOT a middleware, so by the time we run, jwt_claims is
  not yet stamped — without an inline decode we'd never resolve the
  tenant on the very first request after a fresh process start.
* Cache the resolved tuple on ``request.state`` and continue.

The middleware also binds the contextvar in
``saebooks.services.active_company`` so every router's legacy
``_first_company()`` helper resolves to the cookie-selected company
without each callsite needing the request handle (P0-5).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.db import AsyncSessionLocal
from saebooks.services import active_company as active_svc
from saebooks.services import tenant as tenant_svc

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


def _tenant_from_bearer(request: Request) -> uuid.UUID | None:
    """Decode the bearer JWT inline and return its tenant_id claim.

    Returns ``None`` when there's no Authorization header, the header
    isn't a Bearer, the token isn't a valid JWT, or the token is the
    static dev/test token (which has no claims).
    """
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    raw = auth[7:].strip()
    if not raw:
        return None
    # Local import — avoids circular import on app boot.
    from saebooks.services.jwt_tokens import (  # noqa: PLC0415
        JWTError,
        decode_access_token,
    )
    try:
        claims = decode_access_token(raw)
    except JWTError:
        return None
    tenant_raw = claims.get("tenant_id")
    if not tenant_raw:
        return None
    try:
        return uuid.UUID(str(tenant_raw))
    except (TypeError, ValueError):
        return None


class ActiveCompanyMiddleware(BaseHTTPMiddleware):
    """Stash ``request.state.active_company`` + ``companies_for_switcher``."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Tenant resolution: try the ordinary path first (jwt_claims
        # may already be stamped on later middleware in the chain),
        # and fall back to decoding the bearer header inline.
        tenant_id: uuid.UUID | None
        try:
            tenant_id = resolve_tenant_id(request)
        except HTTPException:
            tenant_id = _tenant_from_bearer(request)

        if tenant_id is None:
            return await call_next(request)

        try:
            async with AsyncSessionLocal() as session:
                active, companies = (
                    await active_svc.resolve_active_with_options(
                        session, request, tenant_id
                    )
                )
        except HTTPException:
            active, companies = None, []
        except Exception as exc:  # pragma: no cover - safety net
            _LOG.warning("active company lookup failed: %s", exc)
            active, companies = None, []

        request.state.active_company = active
        request.state.companies_for_switcher = companies

        # Bind the active-company contextvar (legacy _first_company compat)
        # and the ORM scope guard contextvar (services/tenant.py). The
        # scope guard injects WHERE company_id = :cid into CompanyScoped
        # SELECTs, while get_web_session stamps session.info["company_id"]
        # so the after_begin listener issues SET LOCAL app.current_company_id.
        token = active_svc.bind_active_company(active)
        scope_token = tenant_svc.set_current_company(active.id if active else None)
        try:
            return await call_next(request)
        finally:
            active_svc.reset_active_company(token)
            tenant_svc.reset_current_company(scope_token)
