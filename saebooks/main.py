import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from saebooks import __version__
from saebooks.api.errors import register_handlers
from saebooks.api.internal import router as api_internal_router
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
from saebooks.middleware.demo_touch import DemoTouchMiddleware
from saebooks.middleware.request_id import RequestIdMiddleware
from saebooks.middleware.skip_audit import SkipAuditMiddleware
from saebooks.services import metrics as metrics_svc
from saebooks.services import observability, tenant

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
    import asyncio

    logger.info("SAE Books starting (edition=%s)", settings.edition)

    # Job C registration inversion — explicit eager bootstrap so
    # jurisdiction-module registration happens deterministically at
    # startup rather than lazily on first request (the six registry
    # readers also call this, so it is a no-op belt-and-braces call if
    # something races it, but startup is the right place to surface a
    # config error loudly instead of on a live request).
    from saebooks.bootstrap.jurisdictions import ensure_loaded as _ensure_jurisdictions_loaded

    _ensure_jurisdictions_loaded()

    if settings.edition == "community":
        await _assert_single_company()

    # Platform-module key-parity preflight (#32 wave 2). When identity delegation
    # is configured (PLATFORM_BASE_URL set), the module MINTS the login /
    # webauthn / principal-login JWTs that THIS engine then verifies. Prove both
    # containers share SAEBOOKS_SECRET_KEY before trusting delegation; on
    # mismatch / unreachable the preflight disables delegation (identity runs
    # in-process) with a loud ERROR. Fail-open to in-process, never fail-broken.
    if settings.platform_base_url.strip():
        from saebooks.services import platform_client as _platform

        await _platform.verify_key_parity_or_disable()
    # Start the gRPC server alongside uvicorn.
    # Port env vars: SAEBOOKS_REST_PORT (default 8042), SAEBOOKS_GRPC_PORT (default 50051).
    grpc_port = int(os.getenv("SAEBOOKS_GRPC_PORT", "50051"))
    grpc_server = await grpc_serve(grpc_port)

    # Ephemeral demo reaper — in-process 60s background sweep that
    # hard-deletes idle / aged public-preview demo tenants. Only started
    # when DEMO_EPHEMERAL_ENABLED; the stop Event lets the lifespan cancel
    # it cleanly on shutdown. Kept as a plain asyncio task (consistent with
    # the codebase's create_task background pattern; no scheduler needed for
    # a single fixed-interval sweep).
    reaper_stop: asyncio.Event | None = None
    reaper_task: asyncio.Task | None = None
    if settings.demo_ephemeral_enabled:
        from saebooks.services import ephemeral_demo

        reaper_stop = asyncio.Event()
        reaper_task = asyncio.create_task(
            ephemeral_demo.run_reaper_loop(reaper_stop)
        )

    # Intercompany REMOTE-relay outbox dispatcher (Phase 3c). Started ONLY when
    # SAEBOOKS_IC_REMOTE_RELAY_ENABLED is True (default OFF) — with the flag off
    # the task is never created and the outbox stays inert. The task drains the
    # outbox, relays signed payloads to the broker, and backs off / DEADs on
    # failure (never auto-reverses the local leg). Cancelled on shutdown,
    # coexisting with the demo reaper task above (independent stop Events).
    ic_relay_stop: asyncio.Event | None = None
    ic_relay_task: asyncio.Task | None = None
    if settings.ic_remote_relay_enabled:
        from saebooks.services.ic_relay.dispatcher import dispatcher_loop

        ic_relay_stop = asyncio.Event()
        ic_relay_task = asyncio.create_task(
            dispatcher_loop(settings=settings, stop=ic_relay_stop)
        )
        logger.info("IC remote-relay dispatcher task started")

    async def _shutdown() -> None:
        if reaper_stop is not None:
            reaper_stop.set()
        if reaper_task is not None:
            try:
                await asyncio.wait_for(reaper_task, timeout=5)
            except TimeoutError:
                reaper_task.cancel()
        if ic_relay_stop is not None:
            ic_relay_stop.set()
        if ic_relay_task is not None:
            try:
                await asyncio.wait_for(ic_relay_task, timeout=5)
            except TimeoutError:
                ic_relay_task.cancel()
        await grpc_server.stop(grace=5)

    # MCP streamable-HTTP session manager — must run inside an async
    # context so its anyio task group is initialised. Without this the
    # mounted /mcp endpoint 500s with "Task group is not initialized."
    # Only enter the context when the mount is enabled, so the dep
    # never fires on stripped-down deployments.
    if os.getenv("SAEBOOKS_MCP_ENABLED", "1") == "1":
        try:
            from saebooks.mcp.server import mcp as _mcp
            async with _mcp.session_manager.run():
                yield
        except Exception as exc:  # pragma: no cover — fall back to non-MCP
            logger.warning("MCP session manager unavailable: %s", exc)
            yield
    else:
        yield
    await _shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="SAE Books",
        version=__version__,
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
    # SkipAuditMiddleware honours X-Dev-Skip-Audit on developer-tier
    # admin requests; short-circuits change_log writes for that request.
    app.add_middleware(SkipAuditMiddleware)
    # DemoTouchMiddleware bumps an ephemeral demo tenant's last_seen_at on
    # each authenticated request so the reaper measures real idle time. Added
    # last so it sits OUTSIDE ForwardAuthMiddleware and therefore observes the
    # jwt_claims that ForwardAuth stamps during call_next. No-op fast path when
    # DEMO_EPHEMERAL_ENABLED is false.
    app.add_middleware(DemoTouchMiddleware)

    @app.get("/")
    async def root() -> RedirectResponse:
        # The engine is a pure API service (the embedded HTML UI was
        # retired in #32 — saebooks-web is now the only bookkeeper).
        # Point the bare root at the interactive OpenAPI docs so a human
        # hitting the host in a browser lands somewhere useful.
        return RedirectResponse("/docs", status_code=302)

    # Cat-C (W6): stable Stripe webhook at /webhooks/stripe. Not under /api/v1/
    # because Stripe webhook URLs are registered in the Dashboard once and must
    # not change on API version bumps. Auth is HMAC-only.
    app.include_router(_stripe_webhook_router)
    # Phase 0 JSON API surface. Mounted last so its /api/v1/* paths
    # can't clash with any future top-level Jinja route. Bearer-auth
    # gated per-router (see saebooks/api/v1/auth.py) — independent
    # from the HTML JWT middleware above (different decode path).
    app.include_router(api_v1_router)
    # Internal-only surface (NOT under /api/v1, NOT public): the ephemeral
    # demo provisioning endpoint the saebooks-web container calls over the
    # docker network. Stripped from the published OpenAPI below and guarded by
    # a shared secret. The public edge routes only the web container, so this
    # is unreachable from a browser through the tunnel.
    app.include_router(api_internal_router)

    # Native MCP endpoint at /mcp — every SAE Books instance speaks
    # the Model Context Protocol so Claude / ChatGPT / n8n can drive
    # the ledger without a separate container. Auth is the same
    # ``saebk_*`` Bearer used by the REST API (issued at
    # ``/admin/api-tokens``). Tool calls loop back to the local REST
    # listener so ``require_bearer`` resolves the token, hydrates
    # user + tenant_id, and binds RLS on the session. See
    # ``saebooks/mcp/server.py`` for the 145-tool registry.
    #
    # ForwardAuthMiddleware skips /mcp (see OPEN_PATH_PREFIXES) so the
    # MCP transport's Authorization header isn't double-handled.
    if os.getenv("SAEBOOKS_MCP_ENABLED", "1") == "1":
        try:
            from saebooks.mcp.server import streamable_http_asgi_app
            app.mount("/mcp", streamable_http_asgi_app())
            logger.info("MCP endpoint mounted at /mcp")
        except Exception as exc:  # pragma: no cover — don't crash boot
            logger.warning("MCP endpoint unavailable: %s", exc)

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
            if not (
                path.startswith("/admin/")
                or path.startswith("/api/v1/admin/")
                or path.startswith("/mcp")
                or path.startswith("/internal/")
            )
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
