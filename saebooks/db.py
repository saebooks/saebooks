from __future__ import annotations

import logging
import os
import uuid as _uuid
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

# Side-effect import: registers @compiles hooks so postgresql.JSONB / ARRAY
# render as JSON on SQLite (used by both model columns and the inline
# postgresql.JSONB() references in alembic migrations).
from saebooks import db_types  # noqa: F401
from saebooks.config import settings

_log = logging.getLogger("saebooks.db")

# SAEBOOKS_ENV values that are allowed to silently fall back to the
# BYPASSRLS owner role when SAEBOOKS_APP_DATABASE_URL is unset.
# Anything else (production, staging, prod, "") is treated as
# production and fails fast — see tests/db/test_runtime_database_url_strict.py.
_FALLBACK_ALLOWED_ENVS: frozenset[str] = frozenset({"dev", "test", "ci"})


def _runtime_database_url() -> str:
    """Pick the URL the request-time engine should connect with.

    Preference order (P0 cross-tenant leak fix — see migration
    0056_split_db_role.py):

    1. ``SAEBOOKS_APP_DATABASE_URL`` if set — explicit non-superuser role.
    2. ``DATABASE_URL`` — fallback in dev/test/ci only, with a WARNING.
    3. ``RuntimeError`` in production / unknown env — refuse to boot
       rather than serve traffic through the BYPASSRLS owner role
       where ``FORCE ROW LEVEL SECURITY`` is a no-op.

    The value chosen here governs RLS enforcement: if the URL points at
    a superuser or a role with ``BYPASSRLS``, FORCE row security is a
    no-op and tenant isolation collapses to the application-layer
    filters only.

    SQLite (``sqlite+aiosqlite://...``) is the Cashbook single-tenant
    local backend. ``SAEBOOKS_APP_DATABASE_URL`` is ignored on SQLite —
    there is no second role to split into; tenant isolation is
    enforced at the application layer because Cashbook is one physical
    device = one user.
    """
    if settings.app_database_url and not _url_is_sqlite(settings.app_database_url):
        return settings.app_database_url

    # SQLite fallback is single-tenant — no RLS to enforce, no warning needed.
    if _url_is_sqlite(settings.database_url):
        return settings.database_url

    env = os.environ.get("SAEBOOKS_ENV", "").lower()
    if env in _FALLBACK_ALLOWED_ENVS:
        _log.warning(
            "SAEBOOKS_APP_DATABASE_URL is unset — falling back to DATABASE_URL "
            "(BYPASSRLS owner role). FORCE ROW LEVEL SECURITY is a no-op on this "
            "role; tenant isolation in dev relies on the application-layer "
            "filters only. Do not run production traffic through this engine."
        )
        return settings.database_url

    raise RuntimeError(
        "SAEBOOKS_APP_DATABASE_URL is required when SAEBOOKS_ENV is "
        f"{env!r} (any value other than 'dev'/'test'/'ci' is treated as "
        "production). Set SAEBOOKS_APP_DATABASE_URL to the non-superuser "
        "saebooks_app role so FORCE ROW LEVEL SECURITY actually binds — "
        "see migration 0056_split_db_role.py."
    )


def _url_is_sqlite(url: str) -> bool:
    try:
        return make_url(url).get_backend_name() == "sqlite"
    except Exception:
        return url.startswith("sqlite")


def backend_supports_rls() -> bool:
    """Return True if the configured backend supports Postgres-style RLS.

    All ``SET LOCAL app.current_tenant`` and ``FORCE ROW LEVEL SECURITY``
    work is gated behind this predicate. On SQLite (Cashbook) the gate
    returns False and those calls become no-ops; single-tenant
    isolation is a physical invariant of the device, not a DB
    constraint.

    Note: this checks the *runtime* engine's dialect, not the URL
    string — covers cases where a future driver alias maps to the
    same dialect.
    """
    try:
        return engine.dialect.name == "postgresql"
    except Exception:
        # Fall back to URL inspection if engine not yet bound (shouldn't
        # happen in normal use — kept for safety during module import).
        return not _url_is_sqlite(_runtime_database_url())


def _engine_kwargs_for(url: str) -> dict[str, object]:
    """Return create_async_engine kwargs appropriate for the URL's dialect.

    Pooling: defaults to NullPool on both dialects for backwards-compatible
    behaviour. To enable real pooling on Postgres, set
    ``SAEBOOKS_DB_POOL_SIZE`` to a positive integer in the stack env;
    optionally ``SAEBOOKS_DB_MAX_OVERFLOW`` (default 5). With pooling on,
    the per-request connection-establish cost (TLS + auth handshake,
    typically 50-300ms on cold connect) is amortised across the worker's
    lifetime. Safe because all tenant-scoping uses ``SET LOCAL`` which
    is transaction-scoped and resets on connection release.
    """
    if _url_is_sqlite(url):
        return {
            "echo": False,
            "future": True,
            "poolclass": NullPool,
            "connect_args": {"check_same_thread": False},
        }
    pool_size = int(os.environ.get("SAEBOOKS_DB_POOL_SIZE", "0") or "0")
    if pool_size > 0:
        max_overflow = int(os.environ.get("SAEBOOKS_DB_MAX_OVERFLOW", "5") or "5")
        return {
            "echo": False,
            "future": True,
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_pre_ping": True,
            "pool_recycle": 1800,
        }
    return {"echo": False, "future": True, "poolclass": NullPool}


def _register_sqlite_pragmas_and_funcs(eng: AsyncEngine) -> None:
    """Wire SQLite connect-time setup onto an engine.

    Three jobs per connection:

    1. ``PRAGMA foreign_keys = ON`` — SQLite default is OFF, which
       would let every FK in the schema silently no-op. The accounting
       invariants (journal_line.account_id, payment_allocation
       targets) only hold up if FKs are enforced.
    2. Register ``gen_random_uuid()`` as a Python function returning a
       UUID4 string. ~33 alembic migrations use this as a column
       server default; without the function they fail at insert time.
    3. Register ``set_config(name, value, is_local)`` and
       ``current_setting(name, missing_ok)`` as no-op stubs. These
       are Postgres GUC helpers used in cli + wizard SQL — on SQLite
       there is no GUC, but the helpers must exist for query
       parser purposes. ``current_setting`` returns NULL which makes
       any ``::uuid`` cast fail loudly if it accidentally fires on a
       SQLite session; that''s the intended behaviour — RLS is not a
       thing here and the call sites should be guarded.

    Listener is attached to the synchronous engine underneath the
    AsyncEngine (the "sync_engine" attribute exists on every
    AsyncEngine since SQLAlchemy 2.0).
    """

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA foreign_keys = ON")
        finally:
            cur.close()
        dbapi_connection.create_function(
            "gen_random_uuid", 0, lambda: str(_uuid.uuid4())
        )
        # set_config / current_setting kept as defensive stubs so SQL
        # parsing of mixed-dialect statements doesn''t error before
        # the call-site guard kicks in. Real RLS enforcement requires
        # Postgres; see backend_supports_rls().
        dbapi_connection.create_function(
            "set_config", 3, lambda _k, v, _is_local: v
        )
        dbapi_connection.create_function(
            "current_setting", 2, lambda _k, _missing_ok: None
        )


_RUNTIME_URL = _runtime_database_url()
engine = create_async_engine(_RUNTIME_URL, **_engine_kwargs_for(_RUNTIME_URL))
if _url_is_sqlite(_RUNTIME_URL):
    _register_sqlite_pragmas_and_funcs(engine)

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
#
# On SQLite, AppSessionLocal stays None — the CLI is a multi-tenant
# walker and has no analogue on a single-tenant local DB.

_app_role_engine = (
    create_async_engine(
        settings.app_database_url,
        **_engine_kwargs_for(settings.app_database_url),
    )
    if settings.app_database_url and not _url_is_sqlite(settings.app_database_url)
    else None
)

AppSessionLocal: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(
        _app_role_engine, expire_on_commit=False, class_=AsyncSession
    )
    if _app_role_engine is not None
    else None
)


# ---------------------------------------------------------------------- #
# Pre-auth lookup engine (BYPASSRLS)                                     #
# ---------------------------------------------------------------------- #
# ``LoginSessionLocal`` is used ONLY for pre-authentication lookups that
# happen before we know which tenant the request belongs to:
#
#   * POST /auth/login                — resolve user by email to verify pw
#   * POST /auth/signup               — uniqueness check on email
#   * POST /auth/verify-email,
#     /reset-password, /magic-link    — token/email-based lookups
#
# These lookups MUST hit the BYPASSRLS owner role because the runtime
# ``saebooks_app`` role is subject to FORCE-RLS on the ``users`` table,
# whose ``tenant_isolation`` policy reads ``app.current_tenant`` — which
# is unset at login time. Without BYPASSRLS the SELECT silently returns
# zero rows and every login flow 401s with "Invalid credentials"
# regardless of the password.
#
# The owner-role URL is ``DATABASE_URL``, the same one ``saebooks.cli``
# and Alembic use for schema work.
_owner_role_engine = create_async_engine(
    settings.database_url, echo=False, future=True, poolclass=NullPool
)

LoginSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _owner_role_engine, expire_on_commit=False, class_=AsyncSession
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


def _reference_connect_args(url: str) -> dict[str, object]:
    """Per-dialect connect args for the read-only reference engine.

    Postgres path: belt-and-braces ``default_transaction_read_only = on``
    so even if the role grants drift, the transaction itself refuses
    writes.

    SQLite path: ``check_same_thread=False`` for aiosqlite. Read-only
    enforcement on SQLite would normally use ``mode=ro`` in the URI;
    leaving it to the caller since reference data on SQLite is
    typically loaded once at first launch.
    """
    if _url_is_sqlite(url):
        return {"check_same_thread": False}
    return {"server_settings": {"default_transaction_read_only": "on"}}


_reference_engine = (
    create_async_engine(
        settings.reference_database_url,
        echo=False,
        future=True,
        poolclass=NullPool,
        connect_args=_reference_connect_args(settings.reference_database_url),
    )
    if settings.reference_database_url
    else None
)
if _reference_engine is not None and _url_is_sqlite(settings.reference_database_url):
    _register_sqlite_pragmas_and_funcs(_reference_engine)

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
        **_engine_kwargs_for(settings.reference_migration_database_url),
    )
    if settings.reference_migration_database_url
    else None
)
if _reference_migration_engine is not None and _url_is_sqlite(
    settings.reference_migration_database_url
):
    _register_sqlite_pragmas_and_funcs(_reference_migration_engine)

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


async def bootstrap_schema(engine_to_init: AsyncEngine | None = None) -> None:
    """Create all ORM-declared tables on ``engine_to_init``.

    Cashbook / SQLite-backend story: the 100+ alembic migrations
    written for the Postgres ledger are not portable to SQLite. They
    use features SQLite cannot express (ROW LEVEL SECURITY, FORCE RLS,
    CREATE POLICY, CREATE ROLE, SECURITY DEFINER, sequences, PG ENUM
    types, ALTER TABLE forms beyond ADD COLUMN, …). Rewriting each one
    for cross-dialect compatibility is a massive surface and changes
    the migrations' intent — which the architecture decision (see
    [[saebooks-mobile-architecture]] §"two-backend rule") explicitly
    forbade.

    Instead, SQLite consumers (the Cashbook mobile app via the Rust
    core, the test suite when DATABASE_URL is sqlite+aiosqlite://...,
    any local dev box that wants to skip Postgres) build their schema
    from ``Base.metadata.create_all`` directly. This is safe because:

    * SQLite Cashbook is single-tenant by device — no RLS to enforce
      and no multi-role split. The "tenant" is whoever holds the phone.
    * The ORM model is the source of truth for column shapes already
      (Postgres alembic migrations are kept in lock-step via
      ``alembic check`` in CI).
    * No upgrade path is required: when the schema changes, the next
      mobile-app release re-runs ``bootstrap_schema`` against the
      device DB. Existing data is migrated by the Rust core's own
      versioned snapshot logic, not by alembic.

    Calling against Postgres is allowed (e.g. test harness setup) but
    discouraged — Postgres should always use ``alembic upgrade head``
    so the migration history matches production.

    Side effects:

    * Imports every module under ``saebooks.models.*`` to ensure each
      class registers with ``Base.metadata`` before ``create_all``
      runs. Some modules are not re-exported from
      ``saebooks.models.__init__`` (invoice, bill, recurring_invoice)
      but are still part of the ORM — walking the package catches them.
    * Registers SQLite pragmas / Python functions on the engine if it
      is a SQLite engine and the listener was not already wired (idempotent
      via SQLAlchemy event de-dup).
    """
    import importlib
    import pkgutil

    import saebooks.models as _models

    for mod_info in pkgutil.iter_modules(_models.__path__):
        importlib.import_module(f"saebooks.models.{mod_info.name}")

    target = engine_to_init if engine_to_init is not None else engine
    is_sqlite = _url_is_sqlite(str(target.url))
    if is_sqlite:
        _register_sqlite_pragmas_and_funcs(target)
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # On SQLite, seed the rows that Postgres migrations would have
    # seeded:
    # * Default tenant (0040_tenants) — required because the dev/test
    #   static tenant_id '00000000-0000-0000-0000-000000000001' is
    #   FK-referenced by every tenant_id column.
    # The seed uses the ORM model so the UUID is stored in the same
    # 32-char-hex form SQLAlchemy's postgresql.UUID type emits on
    # SQLite (without dashes). Idempotent via uniqueness check.
    if is_sqlite:
        import uuid as _uuid_seed

        from saebooks.models.tenant import Tenant

        AsyncSession_local = async_sessionmaker(
            target, expire_on_commit=False, class_=AsyncSession
        )
        default_tid = _uuid_seed.UUID("00000000-0000-0000-0000-000000000001")
        async with AsyncSession_local() as session:
            existing = await session.get(Tenant, default_tid)
            if existing is None:
                session.add(Tenant(id=default_tid, name="Default", slug="default"))
                await session.commit()


__all__ = [
    "AppSessionLocal",
    "AsyncSessionLocal",
    "Base",
    "CompanySession",
    "ReferenceBase",
    "ReferenceMigrationSession",
    "ReferenceNotConfiguredError",
    "ReferenceSession",
    "backend_supports_rls",
    "bootstrap_schema",
    "engine",
    "get_reference_session",
    "get_session",
]
