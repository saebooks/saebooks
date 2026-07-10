"""Cross-tenant RLS probe for ``scheduled_backup_configs`` and
``scheduled_backup_runs`` (migration 0185, planned-modules Wave E).

Mirrors ``tests/test_rls_inbox_documents.py``'s structure (the
established two-tier testing convention: SQLite-runnable app-layer
proofs live in ``tests/services/test_backup_export.py``; this file is
the Postgres-only, DB-level RLS proof for the two brand-new tables
themselves). Proves — LIVE, against a migrated Postgres — that both
tables carry ``relrowsecurity`` AND ``relforcerowsecurity`` plus a
``tenant_isolation`` policy in the canonical predicate, that a
NOBYPASSRLS ``saebooks_app`` session scoped to tenant A sees ONLY
tenant A's rows, that no GUC means zero rows (deny by default), and
that WITH CHECK blocks writing a row stamped with a foreign tenant_id.

Neither table has a company_id / tenant-coherence trigger (see
``models/scheduled_backup_config.py`` docstring — there's no child FK
to a company row to keep coherent), so this file omits that class of
test relative to the 0174 precedent.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import engine as _owner_engine
from saebooks.models.scheduled_backup_config import ScheduledBackupConfig
from saebooks.models.scheduled_backup_run import ScheduledBackupRun
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"

_TABLES = ("scheduled_backup_configs", "scheduled_backup_runs")


def _resolve_app_url() -> str:
    url = _owner_engine.url.set(username="saebooks_app", password=_APP_ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


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
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with one config row and one run row, inserted
    through the BYPASSRLS owner engine."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"suffix": suffix}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"SBACK185-{label}-{suffix}",
                    slug=f"sback185-{label}-{suffix}",
                )
            )
            await session.flush()
            config = ScheduledBackupConfig(
                id=uuid.uuid4(),
                tenant_id=tid,
                enabled=True,
                destination_type="local_path",
                destination_params={"relative_path": f"{label}-{suffix}"},
            )
            session.add(config)
            await session.flush()
            run = ScheduledBackupRun(
                id=uuid.uuid4(),
                tenant_id=tid,
                config_id=config.id,
                status="SUCCESS",
                destination_type="local_path",
                artifact_path=f"/tmp/{label}-{suffix}.enc",
                artifact_size_bytes=1234,
                artifact_sha256="0" * 64,
            )
            session.add(run)
            await session.flush()
            out[label] = {"tenant_id": tid, "config_id": config.id, "run_id": run.id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text("DELETE FROM scheduled_backup_runs WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
            await session.execute(
                text(
                    "DELETE FROM scheduled_backup_configs WHERE tenant_id = :tid"
                ).bindparams(tid=out[label]["tenant_id"])
            )
        await session.execute(
            text("DELETE FROM tenants WHERE slug LIKE :pat").bindparams(
                pat=f"sback185-%-{suffix}"
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 1. Catalog facts — ENABLE + FORCE + policy, both tables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", _TABLES)
async def test_rls_enabled_and_forced(table: str) -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :t"
                ).bindparams(t=table)
            )
        ).one()
    assert row.relrowsecurity is True, f"{table}: ROW LEVEL SECURITY not enabled"
    assert row.relforcerowsecurity is True, f"{table}: FORCE ROW LEVEL SECURITY missing"


@pytest.mark.parametrize("table", _TABLES)
async def test_tenant_isolation_policy_predicate(table: str) -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_get_expr(polqual, polrelid) AS qual, "
                    "pg_get_expr(polwithcheck, polrelid) AS with_check "
                    "FROM pg_policy WHERE polname = 'tenant_isolation' "
                    "AND polrelid = :t ::regclass"
                ).bindparams(t=table)
            )
        ).one_or_none()
    assert row is not None, f"{table}: tenant_isolation policy missing"
    for expr in (row.qual, row.with_check):
        assert "app.current_tenant" in expr
        assert "tenant_id" in expr


# ---------------------------------------------------------------------------
# 2-4. Live cross-tenant probes through the NOBYPASSRLS app role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", _TABLES)
async def test_tenant_a_sees_only_its_rows(
    app_engine: Any, seeded: dict[str, Any], table: str
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    id_field = "config_id" if table == "scheduled_backup_configs" else "run_id"
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        ids = {row.id for row in await conn.execute(text(f"SELECT id FROM {table}"))}
    assert a[id_field] in ids
    assert b[id_field] not in ids


@pytest.mark.parametrize("table", _TABLES)
async def test_foreign_row_invisible_by_id(
    app_engine: Any, seeded: dict[str, Any], table: str
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    id_field = "config_id" if table == "scheduled_backup_configs" else "run_id"
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(b["tenant_id"])
            )
        )
        found = (
            await conn.execute(
                text(f"SELECT id FROM {table} WHERE id = CAST(:i AS uuid)").bindparams(
                    i=str(a[id_field])
                )
            )
        ).first()
    assert found is None, f"{table}: cross-tenant row leaked through RLS by id probe"


@pytest.mark.parametrize("table", _TABLES)
async def test_no_guc_sees_zero_rows(
    app_engine: Any, seeded: dict[str, Any], table: str
) -> None:
    async with app_engine.connect() as conn:
        count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    assert count == 0, f"{table}: deny-by-default violated — rows visible with no tenant GUC"


async def test_with_check_blocks_foreign_tenant_write_on_configs(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            await conn.execute(
                text(
                    "INSERT INTO scheduled_backup_configs "
                    "(tenant_id, destination_type) "
                    "VALUES (CAST(:tid AS uuid), 'local_path')"
                ).bindparams(tid=str(b["tenant_id"]))
            )


async def test_with_check_blocks_foreign_tenant_write_on_runs(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            await conn.execute(
                text(
                    "INSERT INTO scheduled_backup_runs "
                    "(tenant_id, status, destination_type) "
                    "VALUES (CAST(:tid AS uuid), 'PENDING', 'local_path')"
                ).bindparams(tid=str(b["tenant_id"]))
            )
