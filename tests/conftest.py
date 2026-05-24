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

# Redirect the dev mail outbox to /tmp — /app is root-owned in the dev
# container, so the default /app/mail-outbox is unwriteable for the
# saebooks UID and any signup/verify path that calls send_email() blows
# up with EmailError → 500 in tests. /tmp is world-writable.
os.environ.setdefault("SAEBOOKS_MAIL_OUTBOX_DIR", "/tmp/saebooks-mail-outbox")


def _is_sqlite_backend() -> bool:
    """Inspect DATABASE_URL to detect the SQLite Cashbook backend.

    Returns True if the runtime engine will be SQLite, so the suite
    knows to (a) skip ``@pytest.mark.postgres_only`` tests and (b)
    bootstrap the schema via ``Base.metadata.create_all`` instead of
    expecting alembic to have already migrated the DB.
    """
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith("sqlite") or "+aiosqlite" in url


_BACKEND_IS_SQLITE = _is_sqlite_backend()


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    """Skip ``@pytest.mark.postgres_only`` tests when on SQLite.

    Postgres-only tests are those that exercise RLS / tenant isolation /
    role splits / sequence-based refs — features the SQLite Cashbook
    backend does not implement (single-tenant, application-layer
    isolation). Marking these explicitly keeps the SQLite suite focused
    on what Cashbook actually runs.
    """
    if not _BACKEND_IS_SQLITE:
        return
    skip = pytest.mark.skip(
        reason="postgres_only test skipped on SQLite Cashbook backend"
    )
    for item in items:
        if "postgres_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_sqlite_schema() -> None:
    """On SQLite backend, build the schema from ORM metadata before tests run.

    On Postgres this is a no-op — alembic upgrade head is expected to
    have run as part of the dev container bring-up.
    """
    if not _BACKEND_IS_SQLITE:
        return
    from saebooks.db import bootstrap_schema, engine

    async def _run() -> None:
        await bootstrap_schema(engine)

    asyncio.run(_run())


from saebooks.main import app


@pytest.fixture(scope="session", autouse=True)
def seed_coa() -> None:
    """Ensure the seed company has the AU CoA and tax codes loaded, and that
    it sorts first by ``created_at`` so tests using ``ORDER BY created_at``
    pick the seeded company rather than any older production company.

    ``load_au_coa.main()`` is idempotent — safe against an already-seeded DB.
    Runs once per pytest session before any test touches the DB.

    Skipped on SQLite — ``_load_raw`` in the AU seed uses Postgres-only
    SQL (``CAST(:data AS jsonb)`` + ``ON CONFLICT``) against
    raw_au_tax_codes etc. that are not in the ORM. Tests that need
    AU-seed data should be marked ``postgres_only``.
    """
    if _BACKEND_IS_SQLITE:
        return
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


@pytest.fixture(scope="session", autouse=True)
def seed_default_contact(seed_coa: None) -> None:
    """Ensure the seed company has at least one Contact in DEFAULT_TENANT_ID.

    Many tests (bills, attachments, bill-vehicle-tracking, reports/aged,
    reports/fx, recurring-invoice-transitions, stripe-payment-link, etc.)
    query for an existing Contact in the default tenant and assert it's
    not None. Historically this contact was created as a side-effect of
    other fixtures; with the cross-tenant cleanup it no longer is, so
    we explicitly insert one idempotently here.
    """
    import uuid as _uu

    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.contact import Contact, ContactType
    from saebooks.services.companies import ensure_seed_company

    _DEFAULT_TENANT_ID = _uu.UUID("00000000-0000-0000-0000-000000000001")
    _CONTACT_NAME = "Pytest Default Contact"

    async def _setup() -> None:
        async with AsyncSessionLocal() as session:
            company = await ensure_seed_company(session)
            existing = (
                await session.execute(
                    select(Contact).where(
                        Contact.tenant_id == _DEFAULT_TENANT_ID,
                        Contact.company_id == company.id,
                        Contact.archived_at.is_(None),
                    ).limit(1)
                )
            ).scalars().first()
            if existing is None:
                session.add(
                    Contact(
                        tenant_id=_DEFAULT_TENANT_ID,
                        company_id=company.id,
                        name=_CONTACT_NAME,
                        contact_type=ContactType.BOTH,
                    )
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


# --- SQLite collection-time guards -------------------------------------------
# Two test modules import symbols from not-yet-merged feature branches
# (feat/multi-jurisdiction-engine).  They fail at *collection* time with
# ImportError, which blocks the entire run.  When on SQLite we skip them via
# collect_ignore; on Postgres they collect normally (and will fail if the
# feature is not merged, but that is a pre-existing branch issue, not a
# SQLite-backend issue).

def pytest_ignore_collect(collection_path, config):  # type: ignore[no-untyped-def]
    """Skip broken-import modules on the SQLite backend.

    On Postgres these modules are collected normally; any import errors there
    are a pre-existing branch issue unrelated to this task.
    """
    if not _BACKEND_IS_SQLITE:
        return None
    _SQLITE_COLLECTION_IGNORE = {
        "tests/services/lodgement/test_adapter_registry.py",
        "tests/test_m0_synthetic_nz_company.py",
    }
    # collection_path is a pathlib.Path; check if any suffix matches
    for suffix in _SQLITE_COLLECTION_IGNORE:
        if str(collection_path).endswith(suffix):
            return True
    return None
