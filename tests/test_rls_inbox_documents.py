"""Cross-tenant RLS probe for ``inbox_documents`` (migration 0174).

Document Inbox phase 0 (spec issue #33 §2, RLS checklist item 5): the
probe test ships in the SAME commit as the migration that creates the
table. Proves — LIVE, against a migrated Postgres — that:

1. ``inbox_documents`` carries ``relrowsecurity`` AND
   ``relforcerowsecurity`` plus a ``tenant_isolation`` policy in the
   canonical one-policy-shape-for-the-whole-DB predicate.
2. A NOBYPASSRLS ``saebooks_app`` session scoped to tenant A sees ONLY
   tenant A's rows; tenant B's row is invisible both in a full scan and
   when probed directly by its primary key (the "404 by id" fact the
   API layer builds on).
3. With no ``app.current_tenant`` GUC set, the app role sees zero rows
   — deny by default.
4. WITH CHECK blocks writing a row stamped with a foreign tenant_id.
5. The 0174 NULL-tolerant tenant-coherence trigger: NULL company_id
   inserts pass; a company belonging to another tenant is refused.

Reuses the ``saebooks_app`` role-flip engine pattern from
``tests/test_rls_capture_schema.py`` (0173) /
``tests/test_rls_preaccounting_schema.py`` (0172), but resolves the app
engine URL host/port from the owner engine (rather than hardcoding
``db:5432``) so the probe also runs against a non-compose throwaway
Postgres.
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
from saebooks.models.company import Company
from saebooks.models.inbox_document import InboxDocument
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"

_TABLE = "inbox_documents"


def _resolve_app_url() -> str:
    """Owner URL with the credentials swapped to the saebooks_app role.

    Keeps host/port/database from the live owner engine so the probe
    works both inside the compose stack (host ``db``) and against a
    local throwaway Postgres on a mapped port.
    """
    url = _owner_engine.url.set(
        username="saebooks_app", password=_APP_ROLE_PASSWORD
    )
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


def _doc_kwargs(tenant_id: uuid.UUID, label: str, suffix: str) -> dict[str, Any]:
    return dict(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        vault_file_id=uuid.uuid4(),
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,  # 64 hex chars
        filename=f"receipt-{label}-{suffix}.jpg",
        mime="image/jpeg",
        size_bytes=1234,
        source="UPLOAD",
    )


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, one inbox document each (NULL company_id — the
    arrival state), inserted through the BYPASSRLS owner engine."""
    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"suffix": suffix}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"INBOX174-{label}-{suffix}",
                    slug=f"inbox174-{label}-{suffix}",
                )
            )
            await session.flush()
            doc = InboxDocument(**_doc_kwargs(tid, label, suffix))
            session.add(doc)
            await session.flush()
            out[label] = {"tenant_id": tid, "doc_id": doc.id}
        # A company owned by tenant B — for the coherence-trigger probe.
        cid_b = uuid.uuid4()
        session.add(
            Company(
                id=cid_b,
                tenant_id=out["tenant_b"]["tenant_id"],
                name=f"INBOX174-co-b-{suffix}",
                base_currency="AUD",
                fin_year_start_month=7,
            )
        )
        await session.commit()
        out["tenant_b"]["company_id"] = cid_b
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
        await session.execute(
            text("DELETE FROM companies WHERE id = :cid").bindparams(
                cid=out["tenant_b"]["company_id"]
            )
        )
        await session.execute(
            text("DELETE FROM tenants WHERE slug LIKE :pat").bindparams(
                pat=f"inbox174-%-{suffix}"
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 1. Catalog facts — ENABLE + FORCE + policy
# ---------------------------------------------------------------------------


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
    assert row.relrowsecurity is True, "ROW LEVEL SECURITY not enabled"
    assert row.relforcerowsecurity is True, (
        "FORCE ROW LEVEL SECURITY missing — the 0091 wizard_state regression"
    )


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
    assert row is not None, "tenant_isolation policy missing"
    for expr in (row.qual, row.with_check):
        assert "app.current_tenant" in expr
        assert "tenant_id" in expr


# ---------------------------------------------------------------------------
# 2-4. Live cross-tenant probes through the NOBYPASSRLS app role
# ---------------------------------------------------------------------------


async def test_tenant_a_sees_only_its_rows(
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
        ids = {
            row.id
            for row in await conn.execute(text(f"SELECT id FROM {_TABLE}"))
        }
    assert a["doc_id"] in ids
    assert b["doc_id"] not in ids


async def test_foreign_row_invisible_by_id(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Scoped to tenant B, tenant A's row is gone even when probed by
    primary key — the DB fact behind the API's 404-by-id contract."""
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
                    f"SELECT id FROM {_TABLE} WHERE id = CAST(:i AS uuid)"
                ).bindparams(i=str(a["doc_id"]))
            )
        ).first()
    assert found is None, "cross-tenant row leaked through RLS by id probe"


async def test_no_guc_sees_zero_rows(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    async with app_engine.connect() as conn:
        count = (
            await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))
        ).scalar_one()
    assert count == 0, "deny-by-default violated: rows visible with no tenant GUC"


async def test_with_check_blocks_foreign_tenant_write(
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
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, vault_file_id, sha256, filename, mime, "
                    " size_bytes, source) "
                    "VALUES (CAST(:tid AS uuid), CAST(:vf AS uuid), :sha, "
                    "        'x.jpg', 'image/jpeg', 1, 'UPLOAD')"
                ).bindparams(
                    tid=str(b["tenant_id"]),
                    vf=str(uuid.uuid4()),
                    sha=uuid.uuid4().hex + uuid.uuid4().hex,
                )
            )


# ---------------------------------------------------------------------------
# 5. Tenant-coherence trigger (0174 NULL-tolerant variant)
# ---------------------------------------------------------------------------


async def test_coherence_trigger_rejects_foreign_company(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """A document stamped with a company belonging to another tenant is
    refused by the trigger (defence-in-depth under the RLS policy —
    within-tenant writes can't smuggle a foreign company either)."""
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        with pytest.raises(DBAPIError, match="tenant_coherence"):
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, company_id, vault_file_id, sha256, filename, "
                    " mime, size_bytes, source) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), "
                    "        CAST(:vf AS uuid), :sha, 'x.jpg', 'image/jpeg', "
                    "        1, 'UPLOAD')"
                ).bindparams(
                    tid=str(a["tenant_id"]),
                    cid=str(b["company_id"]),
                    vf=str(uuid.uuid4()),
                    sha=uuid.uuid4().hex + uuid.uuid4().hex,
                )
            )


async def test_null_company_id_insert_passes(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """The 0174 trigger is NULL-tolerant: an unrouted document (NULL
    company_id) inserts cleanly — 0131's shared function would raise."""
    a = seeded["tenant_a"]
    doc_id = uuid.uuid4()
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} "
                "(id, tenant_id, vault_file_id, sha256, filename, mime, "
                " size_bytes, source) "
                "VALUES (CAST(:i AS uuid), CAST(:tid AS uuid), "
                "        CAST(:vf AS uuid), :sha, 'y.jpg', 'image/jpeg', "
                "        1, 'UPLOAD')"
            ).bindparams(
                i=str(doc_id),
                tid=str(a["tenant_id"]),
                vf=str(uuid.uuid4()),
                sha=uuid.uuid4().hex + uuid.uuid4().hex,
            )
        )
    async with _owner_engine.begin() as conn:
        deleted = await conn.execute(
            text(f"DELETE FROM {_TABLE} WHERE id = CAST(:i AS uuid)").bindparams(
                i=str(doc_id)
            )
        )
    assert deleted.rowcount == 1
