"""FastAPI entrypoint for the pre-accounting module container.

Run with ``uvicorn preaccounting_app.main:app --port 8080``. Exposes ONLY the
pre-accounting surface (no ledger, no gRPC, no LaTeX) under
``/module/preaccounting`` plus an unauthenticated ``/healthz`` liveness probe.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from preaccounting_app.routers import purchase_orders, quotes, time_entries

_MODULE_PREFIX = "/module/preaccounting"


def create_app() -> FastAPI:
    app = FastAPI(
        title="SAE Books — pre-accounting module",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    health = APIRouter()

    @health.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only — no DB round-trip, no auth. Deliberately does NOT
        # report readiness of the shared DB (that is the engine's concern);
        # a module that can serve this route can serve authenticated routes.
        return {"status": "ok", "service": "preaccounting"}

    app.include_router(health)

    module = APIRouter(prefix=_MODULE_PREFIX)
    module.include_router(quotes.router)
    module.include_router(purchase_orders.router)
    module.include_router(time_entries.router)
    app.include_router(module)

    return app


app = create_app()
