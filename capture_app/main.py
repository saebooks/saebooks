"""FastAPI entrypoint for the capture module container (web mode).

Run with ``uvicorn capture_app.main:app --port 8080`` (or ``MODE=web python -m
capture_app``). Exposes ONLY the capture surface (imports wizard + bank-feeds
REST + AI document extraction) under ``/module/capture`` plus an
unauthenticated ``/healthz`` liveness probe. No ledger, no gRPC, no LaTeX.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from capture_app.routers import ai_extraction, bank_feeds, imports

_MODULE_PREFIX = "/module/capture"


def create_app() -> FastAPI:
    app = FastAPI(
        title="SAE Books — capture module",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    health = APIRouter()

    @health.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only — no DB round-trip, no auth. A module that can serve
        # this route can serve authenticated routes; DB readiness is the
        # engine's concern.
        return {"status": "ok", "service": "capture"}

    app.include_router(health)

    module = APIRouter(prefix=_MODULE_PREFIX)
    module.include_router(imports.router)
    module.include_router(bank_feeds.router)
    module.include_router(ai_extraction.router)
    app.include_router(module)

    return app


app = create_app()
