import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from saebooks.api.errors import register_handlers
from saebooks.api.v1 import router as api_v1_router
from saebooks.api.webhooks.stripe import router as _stripe_webhook_router
from saebooks.config import settings
from saebooks.connect_app import (
    ConnectDispatchMiddleware,
    build_connect_app,
)
from saebooks.grpc_server import serve as grpc_serve
from saebooks.middleware.active_company import ActiveCompanyMiddleware
from saebooks.middleware.auth import ForwardAuthMiddleware
from saebooks.middleware.request_id import RequestIdMiddleware
from saebooks.routers import (
    accounts,
    auth,
    assets,
    bank_rules,
    bills,
    budgets,
    contacts,
    credit_notes,
    dashboard,
    distributions,
    health,
    invoices,
    items,
    journal,
    payments,
    projects,
    ranges,
    reconciliation,
    recurring_invoices,
    reports,
    search,
    tax_codes,
    templates,
)
from saebooks.routers.contacts import beneficiaries_router
from saebooks.services import metrics as metrics_svc
from saebooks.services import observability, tenant

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("saebooks")

# Swap to JSON formatting + init Sentry if enabled via env (SAEBOOKS_LOG_JSON,
# SENTRY_DSN). Both are no-ops when their respective env vars are unset,
# so Community builds stay on plain-text logs and never call home.
observability.configure(settings)

# Install the tenant-scope ORM event listener. It's a no-op when the
# company contextvar is unset (single-company default), so flipping
# it on has zero effect on existing call sites. When a future
# TenantMiddleware binds ``current_company_id``, every SELECT touching
# a ``CompanyScoped`` entity gets a ``WHERE company_id = :cid`` clause
# injected defence-in-depth.
tenant.install()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("SAE Books starting (edition=%s)", settings.edition)
    if settings.edition == "community":
        await _assert_single_company()
    # Start the gRPC server alongside uvicorn.
    # Port env vars: SAEBOOKS_REST_PORT (default 8042), SAEBOOKS_GRPC_PORT (default 50051).
    grpc_port = int(os.getenv("SAEBOOKS_GRPC_PORT", "50051"))
    grpc_server = await grpc_serve(grpc_port)
    yield
    await grpc_server.stop(grace=5)


def create_app() -> FastAPI:
    app = FastAPI(
        title="SAE Books",
        version="0.0.1",
        description="Self-hosted double-entry accounting",
        lifespan=lifespan,
    )
    # RequestIdMiddleware generates / propagates X-Request-Id on every
    # request. Register before ForwardAuthMiddleware so the id is
    # available to all downstream middleware and handlers.
    app.add_middleware(RequestIdMiddleware)
    # ForwardAuthMiddleware decodes the session JWT from
    # ``Authorization: Bearer <jwt>`` and stamps ``request.state.user``
    # / ``.role``. It's a no-op on /healthz, /metrics, /static/,
    # /webhooks/, /favicon.ico so uptime probes + webhooks work without
    # a session. Dev override via SAEBOOKS_DEV_USER + SAEBOOKS_DEV_ROLE.
    app.add_middleware(ForwardAuthMiddleware)
    # ActiveCompanyMiddleware reads the active_company_id cookie and binds
    # the chosen company on a contextvar so every router's
    # _first_company() helper resolves to the cookie-selected company
    # rather than the legacy first-by-created-at fallback (P0-5).
    app.add_middleware(ActiveCompanyMiddleware)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=302)

    app.include_router(health.router)
    app.include_router(dashboard.router)
    app.include_router(accounts.router)
    app.include_router(auth.router)
    app.include_router(journal.router)
    app.include_router(templates.router)
    app.include_router(tax_codes.router)
    app.include_router(ranges.router)
    app.include_router(reports.router)
    app.include_router(reconciliation.router)
    app.include_router(beneficiaries_router)
    app.include_router(contacts.router)
    # recurring_invoices mounts at /invoices/recurring — must register
    # BEFORE invoices.router so `/invoices/recurring` beats the catch-all
    # `/invoices/{invoice_id}` path (which would otherwise 422 on UUID
    # coercion of the literal "recurring").
    app.include_router(recurring_invoices.router)
    app.include_router(invoices.router)
    app.include_router(bills.router)
    app.include_router(credit_notes.router)
    app.include_router(payments.router)
    app.include_router(projects.router)
    app.include_router(budgets.router)
    app.include_router(distributions.router)
    app.include_router(items.router)
    app.include_router(assets.router)
    app.include_router(bank_rules.router)
    # Global search + /help/shortcuts. No prefix; exposes /search and
    # /help/shortcuts at the top level so the Cmd-K palette fetch call
    # can stay short.
    app.include_router(search.router)
    # Cat-C (W6): stable Stripe webhook at /webhooks/stripe. Not under /api/v1/
    # because Stripe webhook URLs are registered in the Dashboard once and must
    # not change on API version bumps. Auth is HMAC-only.
    app.include_router(_stripe_webhook_router)
    # Phase 0 JSON API surface. Mounted last so its /api/v1/* paths
    # can't clash with any future top-level Jinja route. Bearer-auth
    # gated per-router (see saebooks/api/v1/auth.py) — independent
    # from the HTML JWT middleware above (different decode path).
    app.include_router(api_v1_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Override the OpenAPI schema generator to strip /admin/* paths from
    # the published spec.  The routes still exist and are enforced by
    # require_staff() / require_role() — they just don't advertise
    # themselves as attack targets to unauthenticated spec readers.
    _original_openapi = app.openapi

    def _filtered_openapi() -> dict[str, Any]:
        schema = _original_openapi()
        schema["paths"] = {
            path: item
            for path, item in schema.get("paths", {}).items()
            if not (path.startswith("/admin/") or path.startswith("/api/v1/admin/"))
        }
        return schema

    app.openapi = _filtered_openapi  # type: ignore[method-assign]

    # RFC 7807 Problem Details — convert HTTPException and validation errors
    # to application/problem+json when the caller sets Accept: application/json.
    # HTML routes and browser callers are unaffected.
    register_handlers(app)

    # Prometheus /metrics + per-request latency histogram. Install last
    # so the middleware sits outside every router + mount, capturing
    # real wall-clock latency including static files.
    metrics_svc.install(app)
    return app


async def _assert_single_company() -> None:
    """Community edition: warn (don't crash) if more than one non-archived company exists."""
    from sqlalchemy import text

    from saebooks.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM companies WHERE archived_at IS NULL")
            )
            count = result.scalar_one()
            if count > 1:
                logger.warning(
                    "Community edition: found %d active companies (expected 1). "
                    "Multi-company requires Enterprise edition.",
                    count,
                )
    except Exception as exc:  # table may not exist yet before migrations
        logger.debug("Skipping single-company check: %s", exc)


_fastapi_app = create_app()
# Wrap the FastAPI app with a dispatch middleware that routes
# ``/saebooks.SAEBooks/*`` paths to the Connect-RPC ASGI app and falls
# through to FastAPI for everything else. The Connect server speaks
# gRPC + gRPC-Web + Connect HTTP+JSON from a single handler, sharing
# the same Python process / DB session / observability stack as the
# REST API. See saebooks/connect_app.py.
app = ConnectDispatchMiddleware(_fastapi_app, build_connect_app())
