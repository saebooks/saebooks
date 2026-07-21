"""RLS coverage for ``audit_snapshots`` (migration 0186, Wave C).

What this file proves
----------------------
1. ``relrowsecurity`` + ``relforcerowsecurity`` both ``t``.
2. A ``tenant_isolation`` policy is installed.
3. A NOBYPASSRLS ``saebooks_app`` session scoped to tenant A can read
   tenant A's own snapshot rows.
4. Tenant A's session CANNOT read tenant B's snapshot row — the actual
   cross-tenant leak this migration closes (before 0186 this table had
   NO tenant scoping at all: any row_id guess from any tenant returned
   the row, full before/after financial data included).
5. A NULL-tenant_id row (the settings-table / genuinely-underivable
   case) is invisible to EVERY tenant's SELECT — fail-closed, not a
   leak — but IS insertable by the ``saebooks_app`` role (the
   asymmetric WITH CHECK migration 0186 documents; a strict WITH CHECK
   would break every live settings write).

Test infra mirrors tests/services/bank_feeds/test_rls_bank_feed_accounts.py
(role-flip pattern: owner engine seeds data + asserts structure; a
separate saebooks_app-bound engine proves the policy is real).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

# NOTE: deliberately NOT ``saebooks.db.engine`` — that's the runtime
# engine, which IS the saebooks_app role under --rls (see
# docker-compose.test.yml). This file needs a connection that is always
# the real owner/superuser role (ALTER ROLE below requires it, and the
# catalog probes + URL-template below are clearer reading the one
# genuinely-fixed owner engine rather than a conditionally-app-role one).
from saebooks.db import _owner_role_engine as _owner_engine
from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.models.tenant import Tenant
from tests.conftest import owner_seed_session

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="RLS is a Postgres feature; meaningless on SQLite.",
)


async def _ensure_app_role_login() -> bool:
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            return False
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
    return True


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    ok = await _ensure_app_role_login()
    if not ok:
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}

    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            snap_id = uuid.uuid4()
            session.add(Tenant(id=tid, name=f"RLS186-{label}-{suffix}", slug=f"rls186-{label}-{suffix}"))
            await session.flush()
            session.add(
                AuditSnapshot(
                    id=snap_id,
                    tenant_id=tid,
                    table_name="accounts",
                    row_id=str(uuid.uuid4()),
                    action="update",
                    before_data={"name": "before"},
                    after_data={"name": "after"},
                )
            )
            out[label] = {"tenant_id": tid, "snapshot_id": snap_id}
            await session.flush()

        # A genuinely tenant-less row (mirrors the settings-table case).
        null_id = uuid.uuid4()
        session.add(
            AuditSnapshot(
                id=null_id,
                tenant_id=None,
                table_name="settings",
                row_id="audit_mode",
                action="update",
                before_data={"key": "audit_mode", "value": "immutable"},
            )
        )
        out["null_tenant"] = {"snapshot_id": null_id}
        await session.commit()

    yield out

    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text("DELETE FROM audit_snapshots WHERE id = :id"),
                {"id": out[label]["snapshot_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"), {"tid": out[label]["tenant_id"]}
            )
        await session.execute(
            text("DELETE FROM audit_snapshots WHERE id = :id"),
            {"id": out["null_tenant"]["snapshot_id"]},
        )
        await session.commit()


async def test_audit_snapshots_rls_enabled_and_forced() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'audit_snapshots'"
                )
            )
        ).first()
    assert row is not None, "audit_snapshots missing from pg_class"
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        "migration 0186 either failed or was rolled back"
    )


async def test_audit_snapshots_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT policyname FROM pg_policies "
                    "WHERE tablename = 'audit_snapshots'"
                )
            )
        ).all()
    names = {r.policyname for r in rows}
    assert "tenant_isolation" in names, "0186 policy not installed"


async def test_own_tenant_snapshot_visible(app_engine: Any, seeded: dict[str, Any]) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM audit_snapshots WHERE id = :sid"),
                {"sid": a["snapshot_id"]},
            )
        ).all()
    assert len(visible) == 1, "tenant A could not see its own audit_snapshots row"


async def test_cross_tenant_snapshot_invisible(app_engine: Any, seeded: dict[str, Any]) -> None:
    """The gap 0186 closes: before this migration, audit_snapshots had
    NO tenant scoping at all — any tenant's row_id guess returned
    another tenant's before/after financial data."""
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded["tenant_a"]["tenant_id"]
    b_snapshot = seeded["tenant_b"]["snapshot_id"]

    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM audit_snapshots WHERE id = :sid"),
                {"sid": b_snapshot},
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's audit_snapshots row {b_snapshot} — "
        f"the tenant_isolation policy is broken or not FORCEd"
    )


async def test_null_tenant_snapshot_invisible_to_every_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """A NULL tenant_id row (settings-table snapshots, etc.) must be
    invisible under RLS to every tenant — fail-closed, not a leak."""
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded["tenant_a"]["tenant_id"]
    null_id = seeded["null_tenant"]["snapshot_id"]

    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM audit_snapshots WHERE id = :sid"),
                {"sid": null_id},
            )
        ).all()
    assert len(visible) == 0, "NULL-tenant row must never be SELECT-visible under RLS"


async def test_no_tenant_set_returns_zero(app_engine: Any, seeded: dict[str, Any]) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (
            await session.execute(text("SELECT count(*) FROM audit_snapshots"))
        ).scalar_one()
    assert rows == 0, "deny-by-default: no app.current_tenant GUC must yield zero rows"


async def test_saebooks_app_can_insert_null_tenant_row(app_engine: Any) -> None:
    """The asymmetric WITH CHECK (tenant_id IS NULL OR ...) — proves a
    genuinely tenant-less capture (e.g. services/settings.py's global
    Setting writes) doesn't start 500ing the moment this migration
    ships. A strict copy of the standard tenant_isolation WITH CHECK
    would reject this INSERT outright."""
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    new_id = uuid.uuid4()
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(uuid.uuid4())},  # some tenant, irrelevant to a NULL insert
        )
        await session.execute(
            text(
                "INSERT INTO audit_snapshots "
                "(id, tenant_id, table_name, row_id, action, before_data) "
                "VALUES (:id, NULL, 'settings', 'unit_test_key', 'update', '{}'::jsonb)"
            ),
            {"id": new_id},
        )
    # Cleanup via the owner (bypasses RLS) since the app session can't
    # see the row it just wrote (NULL tenant_id, fails-closed on read).
    async with _owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit_snapshots WHERE id = :id"), {"id": new_id})
