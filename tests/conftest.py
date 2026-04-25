import os

# P0 cross-tenant leak fix: resolve_tenant_id refuses to fall back to
# the dev default tenant outside dev/test. The test suite must set
# SAEBOOKS_ENV before any saebooks module imports so the static-bearer
# code path keeps working.
os.environ.setdefault("SAEBOOKS_ENV", "test")

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.main import app


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
