import asyncio
import os

# P0 cross-tenant leak fix: resolve_tenant_id refuses to fall back to
# the dev default tenant outside dev/test. The test suite must set
# SAEBOOKS_ENV before any saebooks module imports so the static-bearer
# code path keeps working.
os.environ.setdefault("SAEBOOKS_ENV", "test")

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.main import app


@pytest.fixture(scope="session", autouse=True)
def seed_coa() -> None:
    """Ensure the seed company has the AU CoA and tax codes loaded, and that
    it sorts first by ``created_at`` so tests using ``ORDER BY created_at``
    pick the seeded company rather than any older production company.

    ``load_au_coa.main()`` is idempotent — safe against an already-seeded DB.
    Runs once per pytest session before any test touches the DB.
    """
    from sqlalchemy import text

    from saebooks.db import AsyncSessionLocal
    from saebooks.seed.load_au_coa import main as _load_au_coa
    from saebooks.services.companies import ensure_seed_company

    async def _setup() -> None:
        await _load_au_coa()
        # Pin the seed company's created_at to epoch so ORDER BY created_at
        # always selects it first, regardless of when other companies were
        # created in a shared persistent DB.
        async with AsyncSessionLocal() as session:
            company = await ensure_seed_company(session)
            await session.execute(
                text(
                    "UPDATE companies SET created_at = '1970-01-01T00:00:00Z' "
                    "WHERE id = :cid"
                ).bindparams(cid=company.id)
            )
            await session.commit()

    asyncio.run(_setup())


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
