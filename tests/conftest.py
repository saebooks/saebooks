import asyncio
import os

# P0 cross-tenant leak fix: resolve_tenant_id refuses to fall back to
# the dev default tenant outside dev/test. The test suite must set
# SAEBOOKS_ENV before any saebooks module imports so the static-bearer
# code path keeps working.
os.environ.setdefault("SAEBOOKS_ENV", "test")

import os

import pytest
from httpx import ASGITransport, AsyncClient

# Enable middleware's test-only Remote-User trusted-header path before
# importing the app (env reads run on each request, so order doesn't
# matter strictly, but setting it up here keeps it explicit).
os.environ.setdefault("SAEBOOKS_TEST_TRUSTED_USER_HEADER", "1")

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


# Phase G — shared fixtures for routes gated by require_role(ADMIN) /
# require_staff(). Tests that need staff or admin auth use ``admin_client``
# directly, or override their file-local ``client`` fixture to delegate to it.
import uuid as _uuid_mod  # noqa: E402

_PYTEST_ADMIN = "pytest-admin"


@pytest.fixture
async def admin_user() -> str:
    """Ensure the ``pytest-admin`` user exists and has the ADMIN role."""
    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.user import User, UserRole

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == _PYTEST_ADMIN))
        ).scalars().first()
        if existing is None:
            session.add(
                User(
                    tenant_id=_uuid_mod.UUID("00000000-0000-0000-0000-000000000001"),
                    username=_PYTEST_ADMIN,
                    role=UserRole.ADMIN.value,
                )
            )
        else:
            existing.role = UserRole.ADMIN.value
            existing.archived_at = None
        await session.commit()
    return _PYTEST_ADMIN


@pytest.fixture
async def admin_client(admin_user: str) -> AsyncClient:
    """Client pre-injected with ``Remote-User: pytest-admin`` and
    ``SAE_STAFF_USERNAMES=pytest-admin``. Satisfies both
    ``require_role(ADMIN)`` and ``require_staff()`` in one fixture."""
    old = os.environ.get("SAE_STAFF_USERNAMES", "")
    os.environ["SAE_STAFF_USERNAMES"] = _PYTEST_ADMIN
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Remote-User": admin_user},
        ) as ac:
            yield ac
    finally:
        if old:
            os.environ["SAE_STAFF_USERNAMES"] = old
        else:
            os.environ.pop("SAE_STAFF_USERNAMES", None)
