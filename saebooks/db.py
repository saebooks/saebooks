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


# ====================================================================== #
# Reference DB (multi-jurisdiction master data)                          #
# ====================================================================== #
#
# The reference DB is on the same Postgres cluster but in its own
# database. We expose two independent engines:
#
#   reference_engine          — read-only app role for runtime lookups
#   reference_migration_engine — owner role for alembic + seed loader
#
# CompanySession and ReferenceSession are deliberately NOT joined.
# There is no cross-DB FK; validation that a code in the company DB
# resolves to a row in the reference DB happens at the service layer.
#
# Both engines opt out if their URL is unset so dev environments and
# the existing test suite keep working unchanged. Code that needs
# reference data and finds the engine None should raise
# ReferenceNotConfiguredError (defined in services/reference/__init__.py).

# Alias for clarity at call sites — CompanySession is the same engine
# the rest of the app already uses.
CompanySession = AsyncSessionLocal


class ReferenceNotConfiguredError(RuntimeError):
    """Raised when reference DB lookup is attempted but no engine exists."""


_reference_engine = (
    create_async_engine(
        settings.reference_database_url,
        echo=False,
        future=True,
        poolclass=NullPool,
        # Belt-and-braces: the role itself should be NOLOGIN-write, but
        # asking Postgres to also refuse writes at the transaction level
        # turns "I forgot" into a loud error rather than a silent
        # mutation in the rare case the role grants drift.
        connect_args={
            "server_settings": {"default_transaction_read_only": "on"},
        },
    )
    if settings.reference_database_url
    else None
)

ReferenceSession: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(
        _reference_engine, expire_on_commit=False, class_=AsyncSession
    )
    if _reference_engine is not None
    else None
)


_reference_migration_engine = (
    create_async_engine(
        settings.reference_migration_database_url,
        echo=False,
        future=True,
        poolclass=NullPool,
    )
    if settings.reference_migration_database_url
    else None
)

ReferenceMigrationSession: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(
        _reference_migration_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    if _reference_migration_engine is not None
    else None
)


class ReferenceBase(DeclarativeBase):
    """Separate declarative base for reference-DB models.

    Kept apart from ``Base`` so a stray ``Base.metadata.create_all``
    against the company DB cannot create reference tables there, and
    vice-versa. Same reason alembic gets its own env.
    """


async def get_reference_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a reference-DB session.

    Raises if reference DB is not configured. Routes that depend on
    this should be conditionally registered, or the absence reported
    as a 503 at the route layer.
    """
    if ReferenceSession is None:
        raise ReferenceNotConfiguredError(
            "REFERENCE_DATABASE_URL is not configured"
        )
    async with ReferenceSession() as session:
        yield session
