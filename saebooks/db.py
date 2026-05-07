from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from saebooks.config import settings


def _runtime_database_url() -> str:
    """Pick the URL the request-time engine should connect with.

    Preference order (P0 cross-tenant leak fix — see migration
    0056_split_db_role.py):

    1. ``SAEBOOKS_APP_DATABASE_URL`` if set — explicit non-superuser role.
    2. ``DATABASE_URL`` — fallback for dev / single-role setups.

    The value chosen here governs RLS enforcement: if the URL points at
    a superuser or a role with ``BYPASSRLS``, FORCE row security is a
    no-op and tenant isolation collapses to the application-layer
    filters only.
    """
    if settings.app_database_url:
        return settings.app_database_url
    return settings.database_url


engine = create_async_engine(
    _runtime_database_url(), echo=False, future=True, poolclass=NullPool
)

AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


# ---------------------------------------------------------------------- #
# CLI / cron app-role engine                                             #
# ---------------------------------------------------------------------- #
# ``AppSessionLocal`` is the *strict* RLS-enforced sessionmaker used by
# ``python -m saebooks.cli sync-feeds`` (and any other cross-tenant
# CLI walker). Unlike ``AsyncSessionLocal`` above, this one refuses to
# fall back to ``DATABASE_URL`` — if ``SAEBOOKS_APP_DATABASE_URL`` is
# unset, the factory returns ``None`` and the CLI raises at startup.
#
# Rationale: the CLI iterates every tenant, setting ``app.current_tenant``
# per group. If the connection silently used the BYPASSRLS owner role,
# the per-tenant ``SET LOCAL`` would be a no-op and the run would still
# "work" — masking the misconfiguration. Forcing the strict role at
# CLI boot makes the failure mode loud.
#
# The runtime web engine above (``engine`` / ``AsyncSessionLocal``) keeps
# its fallback because dev environments commonly run a single role and
# the FastAPI test suite seeds tenants directly. Once every web router
# is audited (see ``audit-trail/06``-style guard in compose ``.env``)
# the web engine should adopt the same strict pattern.

_app_role_engine = (
    create_async_engine(
        settings.app_database_url,
        echo=False,
        future=True,
        poolclass=NullPool,
    )
    if settings.app_database_url
    else None
)

AppSessionLocal: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(
        _app_role_engine, expire_on_commit=False, class_=AsyncSession
    )
    if _app_role_engine is not None
    else None
)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
