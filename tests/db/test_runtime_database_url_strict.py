"""Strict-mode regression for ``saebooks.db._runtime_database_url``.

Why this test exists
--------------------
Migration 0055 forces RLS on every tenant-scoped table; migration
0056 splits the DB role so the runtime web engine can connect as a
NOSUPERUSER + NOBYPASSRLS role (``saebooks_app``) and have FORCE row
security actually bind.

Until 2026-05-10, ``_runtime_database_url`` silently fell back to
``DATABASE_URL`` (the schema-owner role, which bypasses RLS) when
``SAEBOOKS_APP_DATABASE_URL`` was unset — so a misconfigured prod
deployment would boot, look healthy, and quietly serve cross-tenant
reads through the BYPASSRLS owner. The fallback is now scoped to
``SAEBOOKS_ENV in {dev, test, ci, ""}``; in any other env the function
raises rather than serving traffic with RLS disabled.

These tests pin that contract so a future re-introduction of a
"helpful" fallback fails loudly.
"""
from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.postgres_only



@pytest.fixture(autouse=True)
def _restore_db_module() -> None:
    """Restore ``saebooks.config`` + ``saebooks.db`` after each test.

    The ``monkeypatch`` fixture restores env vars at teardown, but the
    module-level ``settings`` / ``engine`` objects keep their patched
    state until restored. Without this, a downstream test that imports
    ``AsyncSessionLocal`` would get a session bound to the wrong URL.

    We snapshot each module's ``__dict__`` before the test and restore it
    after. ``importlib.reload`` mutates the module object IN PLACE, rebinding
    ``Base``/``ReferenceBase`` to fresh declarative bases with EMPTY metadata
    while the already-imported model classes stay bound to the ORIGINAL bases
    — orphaning the ORM registry. That made every downstream reference-DB
    seed test fail with "Unknown reference table 'jurisdictions'. Known
    tables: []". Restoring the ``__dict__`` snapshot puts the original
    (populated) ``ReferenceBase`` / ``Base`` / engine / session objects back,
    consistent with the model classes. (Restoring the sys.modules entry does
    NOT work — reload mutates the same object in place.)
    """
    import saebooks.config as cfg
    import saebooks.db as db

    orig_cfg = dict(cfg.__dict__)
    orig_db = dict(db.__dict__)
    yield
    cfg.__dict__.clear()
    cfg.__dict__.update(orig_cfg)
    db.__dict__.clear()
    db.__dict__.update(orig_db)


def _reload_db_with_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    """Reload ``saebooks.config`` + ``saebooks.db`` with patched env.

    Pydantic settings cache the env at import time, so we need a
    fresh ``Settings()`` for each scenario.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    import saebooks.config as cfg

    importlib.reload(cfg)
    import saebooks.db as db

    return importlib.reload(db)


def test_app_url_used_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``SAEBOOKS_APP_DATABASE_URL`` always wins."""
    db = _reload_db_with_settings(
        monkeypatch,
        SAEBOOKS_APP_DATABASE_URL="postgresql+asyncpg://saebooks_app:pw@db/saebooks",
        DATABASE_URL="postgresql+asyncpg://saebooks:pw@db/saebooks",
        SAEBOOKS_ENV="production",
    )
    assert (
        db._runtime_database_url()
        == "postgresql+asyncpg://saebooks_app:pw@db/saebooks"
    )


def test_dev_env_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """In dev, missing app URL falls back to DATABASE_URL but logs warning."""
    monkeypatch.delenv("SAEBOOKS_APP_DATABASE_URL", raising=False)
    db = _reload_db_with_settings(
        monkeypatch,
        DATABASE_URL="postgresql+asyncpg://saebooks:pw@db/saebooks",
        SAEBOOKS_ENV="dev",
    )

    with caplog.at_level("WARNING", logger="saebooks.db"):
        url = db._runtime_database_url()

    assert url == "postgresql+asyncpg://saebooks:pw@db/saebooks"
    assert any(
        "BYPASSRLS owner role" in rec.getMessage() for rec in caplog.records
    )


def test_test_env_falls_back_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """SAEBOOKS_ENV=test is treated as dev for fallback purposes."""
    monkeypatch.delenv("SAEBOOKS_APP_DATABASE_URL", raising=False)
    db = _reload_db_with_settings(
        monkeypatch,
        DATABASE_URL="postgresql+asyncpg://saebooks:pw@db/saebooks",
        SAEBOOKS_ENV="test",
    )
    assert (
        db._runtime_database_url()
        == "postgresql+asyncpg://saebooks:pw@db/saebooks"
    )


def test_production_env_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """SAEBOOKS_ENV=production without app URL must raise on module import.

    ``_runtime_database_url`` is called at module-load time to build
    the engine, so a misconfigured prod container fails fast at boot
    rather than serving traffic with RLS disabled.
    """
    monkeypatch.delenv("SAEBOOKS_APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("SAEBOOKS_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://saebooks:pw@db/saebooks"
    )

    import saebooks.config as cfg

    importlib.reload(cfg)
    import saebooks.db as db

    with pytest.raises(RuntimeError, match="SAEBOOKS_APP_DATABASE_URL is required"):
        importlib.reload(db)


def test_unknown_env_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unrecognised env is treated as production (fail-closed)."""
    monkeypatch.delenv("SAEBOOKS_APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("SAEBOOKS_ENV", "staging")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://saebooks:pw@db/saebooks"
    )

    import saebooks.config as cfg

    importlib.reload(cfg)
    import saebooks.db as db

    with pytest.raises(RuntimeError, match="SAEBOOKS_APP_DATABASE_URL is required"):
        importlib.reload(db)
