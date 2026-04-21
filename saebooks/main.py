import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from saebooks.config import settings
from saebooks.middleware.auth import ForwardAuthMiddleware
from saebooks.routers import (
    accounts,
    admin,
    assets,
    bank_feeds,
    bank_rules,
    bills,
    contacts,
    credit_notes,
    dashboard,
    health,
    imports,
    integrations,
    invoices,
    journal,
    pay_run,
    payments,
    ranges,
    reconciliation,
    recurring_invoices,
    reports,
    search,
    tax_codes,
    templates,
)
from saebooks.services import metrics as metrics_svc
from saebooks.services import observability

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("saebooks")

# Swap to JSON formatting + init Sentry if enabled via env (SAEBOOKS_LOG_JSON,
# SENTRY_DSN). Both are no-ops when their respective env vars are unset,
# so Community builds stay on plain-text logs and never call home.
observability.configure(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("SAE Books starting (edition=%s)", settings.edition)
    if settings.edition == "community":
        await _assert_single_company()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="SAE Books",
        version="0.0.1",
        description="Self-hosted double-entry accounting",
        lifespan=lifespan,
    )
    # ForwardAuthMiddleware reads Authentik's Remote-User header (set by
    # Caddy forward-auth) and stamps request.state.user / .role. It's a
    # no-op on /healthz, /metrics, /static/, /webhooks/, /favicon.ico so
    # uptime probes + webhooks work without SSO. Dev override via
    # SAEBOOKS_DEV_USER + SAEBOOKS_DEV_ROLE env vars.
    app.add_middleware(ForwardAuthMiddleware)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=302)

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(dashboard.router)
    app.include_router(accounts.router)
    app.include_router(journal.router)
    app.include_router(templates.router)
    app.include_router(tax_codes.router)
    app.include_router(ranges.router)
    app.include_router(reports.router)
    app.include_router(reconciliation.router)
    # Integrations (LEI/GLEIF lookup, Stripe webhook, Paperless attach,
    # ATO prefill stub). No prefix — routes carry their own mount paths
    # (/contacts/lei-*, /webhooks/stripe, /admin/integrations/*) so they
    # integrate with the contact form + public webhook surface + admin
    # landing. Registered BEFORE contacts.router so /contacts/lei-lookup
    # beats the catch-all /contacts/{contact_id} path (same trick as
    # /invoices/recurring vs /invoices/{invoice_id} below).
    app.include_router(integrations.router)
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
    app.include_router(pay_run.router)
    app.include_router(assets.router)
    app.include_router(bank_feeds.router)
    app.include_router(bank_rules.router)
    app.include_router(imports.router)
    # Global search + /help/shortcuts. No prefix; exposes /search and
    # /help/shortcuts at the top level so the Cmd-K palette fetch call
    # can stay short.
    app.include_router(search.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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


app = create_app()
