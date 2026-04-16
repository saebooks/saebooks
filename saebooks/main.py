import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from saebooks.config import settings
from saebooks.routers import (
    accounts,
    admin,
    contacts,
    health,
    journal,
    ranges,
    reconciliation,
    reports,
    tax_codes,
    templates,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("saebooks")


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
    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/journal", status_code=302)

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(accounts.router)
    app.include_router(journal.router)
    app.include_router(templates.router)
    app.include_router(tax_codes.router)
    app.include_router(ranges.router)
    app.include_router(reports.router)
    app.include_router(reconciliation.router)
    app.include_router(contacts.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
