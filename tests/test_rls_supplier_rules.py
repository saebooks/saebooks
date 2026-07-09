"""Cross-tenant RLS probe for ``supplier_rules`` (migration 0175).

Document Inbox phase 2 (spec issue #33 §2 migration B, RLS checklist
item 5): the probe test ships in the SAME commit as the migration that
creates the table. Proves — LIVE, against a migrated Postgres — that:

1. ``supplier_rules`` carries ``relrowsecurity`` AND
   ``relforcerowsecurity`` plus a ``tenant_isolation`` policy in the
   canonical one-policy-shape-for-the-whole-DB predicate.
2. A NOBYPASSRLS ``saebooks_app`` session scoped to tenant A sees ONLY
   tenant A's rules; tenant B's rule is invisible both in a full scan
   and when probed directly by primary key.
3. With no ``app.current_tenant`` GUC set, the app role sees zero rows
   — deny by default.
4. WITH CHECK blocks writing a rule stamped with a foreign tenant_id.
5. The 0175 NULL-tolerant tenant-coherence trigger: NULL company_id
   (tenant-wide rule) inserts pass; a company belonging to another
   tenant is refused.
6. The hand-written partial expression unique
   (``uq_supplier_rules_scope_vendor``) exists and fires: a second
   ACTIVE rule for the same (tenant, NULL company, vendor_key) is
   refused, while an inactive duplicate is allowed (soft-delete frees
   the slot).

Reuses the ``saebooks_app`` role-flip engine pattern from
``tests/test_rls_inbox_documents.py`` (owner-engine URL resolution, so
the probe runs both in compose and against a throwaway Postgres).
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
from saebooks.models.contact import Contact, ContactType
from saebooks.models.supplier_rule import SupplierRule
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"

_TABLE = "supplier_rules"


def _resolve_app_url() -> str:
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


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company, a contact and one tenant-wide
    supplier rule (NULL company_id), inserted through the BYPASSRLS
    owner engine."""
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
                    name=f"RULES175-{label}-{suffix}",
                    slug=f"rules175-{label}-{suffix}",
                )
            )
            await session.flush()
            cid = uuid.uuid4()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RULES175-co-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            contact = Contact(
                id=uuid.uuid4(),
                tenant_id=tid,
                company_id=cid,
                name=f"RULES175-vendor-{label}-{suffix}",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(contact)
            await session.flush()
            rule = SupplierRule(
                tenant_id=tid,
                company_id=None,  # tenant-wide — the coherence trigger's NULL leg
                vendor_key=f"bp wacol {label} {suffix}",
                contact_id=contact.id,
            )
            session.add(rule)
            await session.flush()
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "contact_id": contact.id,
                "rule_id": rule.id,
            }
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
            await session.execute(
                text("DELETE FROM contacts WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
            await session.execute(
                text("DELETE FROM companies WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
        await session.execute(
            text("DELETE FROM tenants WHERE slug LIKE :pat").bindparams(
                pat=f"rules175-%-{suffix}"
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 1. Catalog facts — ENABLE + FORCE + policy + the hand-written unique
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


async def test_partial_expression_unique_exists() -> None:
    """The hand-written index autogenerate cannot emit: UNIQUE over
    (tenant_id, coalesce(company_id, nil), vendor_key) WHERE active."""
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE tablename = :t "
                    "AND indexname = 'uq_supplier_rules_scope_vendor'"
                ).bindparams(t=_TABLE)
            )
        ).one_or_none()
    assert row is not None, "uq_supplier_rules_scope_vendor missing"
    idx = row.indexdef
    assert "UNIQUE" in idx
    assert "COALESCE" in idx
    assert "WHERE active" in idx


# ---------------------------------------------------------------------------
# 2-4. Live cross-tenant probes through the NOBYPASSRLS app role
# ---------------------------------------------------------------------------


async def test_tenant_a_sees_only_its_rules(
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
    assert a["rule_id"] in ids
    assert b["rule_id"] not in ids


async def test_foreign_rule_invisible_by_id(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
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
                ).bindparams(i=str(a["rule_id"]))
            )
        ).first()
    assert found is None, "cross-tenant rule leaked through RLS by id probe"


async def test_no_guc_sees_zero_rows(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    async with app_engine.connect() as conn:
        count = (
            await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))
        ).scalar_one()
    assert count == 0, "deny-by-default violated: rules visible with no tenant GUC"


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
                    f"INSERT INTO {_TABLE} (tenant_id, vendor_key, contact_id) "
                    "VALUES (CAST(:tid AS uuid), :vk, CAST(:cid AS uuid))"
                ).bindparams(
                    tid=str(b["tenant_id"]),
                    vk=f"smuggled {uuid.uuid4().hex[:8]}",
                    cid=str(b["contact_id"]),
                )
            )


# ---------------------------------------------------------------------------
# 5. Tenant-coherence trigger (0175 NULL-tolerant variant)
# ---------------------------------------------------------------------------


async def test_coherence_trigger_rejects_foreign_company(
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
        with pytest.raises(DBAPIError, match="tenant_coherence"):
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, company_id, vendor_key, contact_id) "
                    "VALUES (CAST(:tid AS uuid), CAST(:co AS uuid), :vk, "
                    "        CAST(:cid AS uuid))"
                ).bindparams(
                    tid=str(a["tenant_id"]),
                    co=str(b["company_id"]),
                    vk=f"foreign-co {uuid.uuid4().hex[:8]}",
                    cid=str(a["contact_id"]),
                )
            )


async def test_null_company_rule_insert_passes(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Tenant-wide rules (NULL company_id) insert cleanly — the trigger
    is NULL-tolerant by design."""
    a = seeded["tenant_a"]
    rule_id = uuid.uuid4()
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} (id, tenant_id, vendor_key, contact_id) "
                "VALUES (CAST(:i AS uuid), CAST(:tid AS uuid), :vk, "
                "        CAST(:cid AS uuid))"
            ).bindparams(
                i=str(rule_id),
                tid=str(a["tenant_id"]),
                vk=f"tenant-wide {uuid.uuid4().hex[:8]}",
                cid=str(a["contact_id"]),
            )
        )
    async with _owner_engine.begin() as conn:
        deleted = await conn.execute(
            text(f"DELETE FROM {_TABLE} WHERE id = CAST(:i AS uuid)").bindparams(
                i=str(rule_id)
            )
        )
    assert deleted.rowcount == 1


# ---------------------------------------------------------------------------
# 6. The partial unique fires — and soft-delete frees the slot
# ---------------------------------------------------------------------------


async def test_active_duplicate_vendor_key_refused_inactive_allowed(
    seeded: dict[str, Any],
) -> None:
    a = seeded["tenant_a"]
    vendor_key = f"dup vendor {uuid.uuid4().hex[:8]}"
    ids: list[uuid.UUID] = []
    try:
        async with _owner_engine.begin() as conn:
            first = uuid.uuid4()
            ids.append(first)
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} (id, tenant_id, vendor_key, contact_id) "
                    "VALUES (CAST(:i AS uuid), CAST(:tid AS uuid), :vk, "
                    "        CAST(:cid AS uuid))"
                ).bindparams(
                    i=str(first),
                    tid=str(a["tenant_id"]),
                    vk=vendor_key,
                    cid=str(a["contact_id"]),
                )
            )
        # Second ACTIVE rule, same scope + vendor_key → unique violation.
        with pytest.raises(DBAPIError, match="uq_supplier_rules_scope_vendor"):
            async with _owner_engine.begin() as conn:
                await conn.execute(
                    text(
                        f"INSERT INTO {_TABLE} (tenant_id, vendor_key, contact_id) "
                        "VALUES (CAST(:tid AS uuid), :vk, CAST(:cid AS uuid))"
                    ).bindparams(
                        tid=str(a["tenant_id"]),
                        vk=vendor_key,
                        cid=str(a["contact_id"]),
                    )
                )
        # Inactive duplicate is fine — soft-delete frees the slot.
        async with _owner_engine.begin() as conn:
            second = uuid.uuid4()
            ids.append(second)
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(id, tenant_id, vendor_key, contact_id, active) "
                    "VALUES (CAST(:i AS uuid), CAST(:tid AS uuid), :vk, "
                    "        CAST(:cid AS uuid), false)"
                ).bindparams(
                    i=str(second),
                    tid=str(a["tenant_id"]),
                    vk=vendor_key,
                    cid=str(a["contact_id"]),
                )
            )
    finally:
        async with _owner_engine.begin() as conn:
            for rid in ids:
                await conn.execute(
                    text(
                        f"DELETE FROM {_TABLE} WHERE id = CAST(:i AS uuid)"
                    ).bindparams(i=str(rid))
                )
