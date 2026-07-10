"""Cross-tenant RLS probe for ``roles`` and ``role_permissions``
(migrations 0190/0194, granular_permissions module).

Mirrors ``tests/test_rls_scheduled_backups.py``'s structure — proves,
LIVE against a migrated Postgres, that both tables carry
``relrowsecurity`` AND ``relforcerowsecurity`` plus a
``tenant_isolation`` policy in the canonical predicate, that a
NOBYPASSRLS ``saebooks_app`` session scoped to tenant A sees ONLY
tenant A's rows, that no GUC means zero rows (deny by default), and
that WITH CHECK blocks writing a row stamped with a foreign tenant_id.
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
from saebooks.models.permission import RolePermission
from saebooks.models.role import Role
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"

_TABLES = ("roles", "role_permissions")


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
    """Two tenants, each with one custom role + one grant row, inserted
    through the BYPASSRLS owner engine. Uses an existing SEEDED
    permission code (dashboard.view) so the FK is always satisfiable
    regardless of migration order."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"suffix": suffix}
    async with Owner() as session:
        code_row = (
            await session.execute(text("SELECT code FROM permissions LIMIT 1"))
        ).first()
        assert code_row is not None, "permissions table is empty — seed migrations missing"
        code = code_row[0]
        out["code"] = code
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"ROLES194-{label}-{suffix}",
                    slug=f"roles194-{label}-{suffix}",
                )
            )
            await session.flush()
            role = Role(
                id=uuid.uuid4(),
                tenant_id=tid,
                name=f"Custom-{label}-{suffix}",
                base_role=None,
                is_system=False,
            )
            session.add(role)
            await session.flush()
            grant = RolePermission(role_id=role.id, tenant_id=tid, permission_code=code)
            session.add(grant)
            await session.flush()
            out[label] = {"tenant_id": tid, "role_id": role.id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text("DELETE FROM role_permissions WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
            await session.execute(
                text("DELETE FROM roles WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
        await session.execute(
            text("DELETE FROM tenants WHERE slug LIKE :pat").bindparams(
                pat=f"roles194-%-{suffix}"
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
    id_field = "role_id"
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        if table == "roles":
            ids = {row.id for row in await conn.execute(text(f"SELECT id FROM {table}"))}
            assert a[id_field] in ids
            assert b[id_field] not in ids
        else:
            role_ids = {
                row.role_id for row in await conn.execute(text(f"SELECT role_id FROM {table}"))
            }
            assert a[id_field] in role_ids
            assert b[id_field] not in role_ids


@pytest.mark.parametrize("table", _TABLES)
async def test_foreign_row_invisible_by_id(
    app_engine: Any, seeded: dict[str, Any], table: str
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(b["tenant_id"])
            )
        )
        if table == "roles":
            found = (
                await conn.execute(
                    text("SELECT id FROM roles WHERE id = CAST(:i AS uuid)").bindparams(
                        i=str(a["role_id"])
                    )
                )
            ).first()
        else:
            found = (
                await conn.execute(
                    text(
                        "SELECT role_id FROM role_permissions "
                        "WHERE role_id = CAST(:i AS uuid)"
                    ).bindparams(i=str(a["role_id"]))
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


async def test_with_check_blocks_foreign_tenant_write_on_roles(
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
                    "INSERT INTO roles (tenant_id, name, is_system) "
                    "VALUES (CAST(:tid AS uuid), 'Hostile Role', false)"
                ).bindparams(tid=str(b["tenant_id"]))
            )


async def test_with_check_blocks_foreign_tenant_write_on_role_permissions(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    code = seeded["code"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            await conn.execute(
                text(
                    "INSERT INTO role_permissions (role_id, tenant_id, permission_code) "
                    "VALUES (CAST(:rid AS uuid), CAST(:tid AS uuid), :code)"
                ).bindparams(rid=str(a["role_id"]), tid=str(b["tenant_id"]), code=code)
            )
