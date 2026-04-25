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


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
