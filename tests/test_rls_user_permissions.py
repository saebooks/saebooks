"""Cross-tenant RLS probe for ``user_permissions.tenant_id``
(migration 0191, granular_permissions module — the "Schema gaps §1"
fix: this table had NO tenant scoping at all before this module).

Mirrors ``tests/test_rls_scheduled_backups.py``'s structure.
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
from saebooks.models.permission import UserPermission
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from tests.conftest import owner_seed_session

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"

_TABLE = "user_permissions"


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
    """Two tenants, each with one user + one per-user permission
    override row, inserted through the BYPASSRLS owner engine."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"suffix": suffix}
    async with owner_seed_session() as session:
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
                    name=f"UPERM191-{label}-{suffix}",
                    slug=f"uperm191-{label}-{suffix}",
                )
            )
            await session.flush()
            user = User(
                id=uuid.uuid4(),
                tenant_id=tid,
                username=f"uperm191-{label}-{suffix}",
                role="bookkeeper",
            )
            session.add(user)
            await session.flush()
            override = UserPermission(
                user_id=user.id, tenant_id=tid, permission_code=code, granted=True
            )
            session.add(override)
            await session.flush()
            out[label] = {"tenant_id": tid, "user_id": user.id}
        await session.commit()
    yield out
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
            await session.execute(
                text("DELETE FROM users WHERE id = :uid").bindparams(
                    uid=out[label]["user_id"]
                )
            )
        await session.execute(
            text("DELETE FROM tenants WHERE slug LIKE :pat").bindparams(
                pat=f"uperm191-%-{suffix}"
            )
        )
        await session.commit()


async def test_rls_enabled_and_forced() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :t"
                ).bindparams(t=_TABLE)
            )
        ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


async def test_tenant_isolation_policy_predicate() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_get_expr(polqual, polrelid) AS qual, "
                    "pg_get_expr(polwithcheck, polrelid) AS with_check "
                    "FROM pg_policy WHERE polname = 'tenant_isolation' "
                    "AND polrelid = :t ::regclass"
                ).bindparams(t=_TABLE)
            )
        ).one_or_none()
    assert row is not None
    for expr in (row.qual, row.with_check):
        assert "app.current_tenant" in expr
        assert "tenant_id" in expr


async def test_tenant_a_sees_only_its_rows(app_engine: Any, seeded: dict[str, Any]) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        user_ids = {
            row.user_id
            for row in await conn.execute(text(f"SELECT user_id FROM {_TABLE}"))
        }
    assert a["user_id"] in user_ids
    assert b["user_id"] not in user_ids


async def test_foreign_row_invisible_by_id(app_engine: Any, seeded: dict[str, Any]) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(b["tenant_id"])
            )
        )
        found = (
            await conn.execute(
                text(
                    f"SELECT user_id FROM {_TABLE} WHERE user_id = CAST(:i AS uuid)"
                ).bindparams(i=str(a["user_id"]))
            )
        ).first()
    assert found is None, "cross-tenant user_permissions row leaked through RLS by id probe"


async def test_no_guc_sees_zero_rows(app_engine: Any, seeded: dict[str, Any]) -> None:
    async with app_engine.connect() as conn:
        count = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    assert count == 0, "deny-by-default violated — rows visible with no tenant GUC"


async def test_with_check_blocks_foreign_tenant_write(
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
                    f"INSERT INTO {_TABLE} (user_id, tenant_id, permission_code, granted) "
                    "VALUES (CAST(:uid AS uuid), CAST(:tid AS uuid), :code, true)"
                ).bindparams(uid=str(a["user_id"]), tid=str(b["tenant_id"]), code=code)
            )
