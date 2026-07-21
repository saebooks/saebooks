"""Cross-tenant RLS probes for ``inbox_email_addresses`` +
``inbox_email_messages`` and the SECURITY DEFINER enumerators
(migration 0176 — Document Inbox phase 3, spec issue #33 §2 migration C).

RLS checklist item 5: the probe test ships in the SAME commit as the
migration. Proves — LIVE, against a migrated Postgres — that:

1. Both tables carry ``relrowsecurity`` AND ``relforcerowsecurity`` plus
   the canonical ``tenant_isolation`` policy.
2. A NOBYPASSRLS ``saebooks_app`` session scoped to tenant A sees ONLY
   tenant A's rows; tenant B's row is invisible in a scan and by id.
3. No GUC → zero rows (deny by default).
4. WITH CHECK blocks writing a row stamped with a foreign tenant_id.
5. The token is GLOBALLY unique — the plain unique constraint fires
   across tenants even though RLS hides the conflicting row.
6. ``inbox_email_addresses_for_poll()`` (SECURITY DEFINER, 0084
   posture) hands the app role the cross-tenant routing map — active
   addresses from EVERY tenant, revoked ones excluded — with no GUC set.
7. ``inbox_documents_tenants_for_sweep()`` enumerates tenants with
   claimable inbox documents cross-tenant for the app role.

Engine-resolution pattern follows ``tests/test_rls_inbox_documents.py``
(app-role URL derived from the live owner engine, not hardcoded).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
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
from saebooks.models.company import Company
from saebooks.models.inbox_document import InboxDocument
from saebooks.models.inbox_email import InboxEmailAddress, InboxEmailMessage
from saebooks.models.tenant import Tenant
from tests.conftest import owner_seed_session

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"

_ADDR = "inbox_email_addresses"
_MSGS = "inbox_email_messages"


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


def _token() -> str:
    """A valid-shape (lowercase base32) unique token."""
    return uuid.uuid4().hex[:12].translate(str.maketrans("0189abcdef", "2345abcdef"))


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with: one ACTIVE email address, one message
    ledger row; tenant B also gets a REVOKED address (must be absent
    from the poll enumerator) and a claimable RECEIVED inbox document
    (for the sweep enumerator). Inserted via the BYPASSRLS owner."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"suffix": suffix}
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"INBOX176-{label}-{suffix}",
                    slug=f"inbox176-{label}-{suffix}",
                )
            )
            await session.flush()
            addr = InboxEmailAddress(tenant_id=tid, token=_token())
            msg = InboxEmailMessage(
                tenant_id=tid,
                mailbox="inbox@in.test",
                message_id=f"<{label}-{suffix}@test>",
                subject=f"probe {label}",
            )
            session.add_all([addr, msg])
            await session.flush()
            out[label] = {
                "tenant_id": tid,
                "addr_id": addr.id,
                "token": addr.token,
                "msg_id": msg.id,
            }
        # Revoked address for tenant B — excluded from the enumerator.
        revoked = InboxEmailAddress(
            tenant_id=out["tenant_b"]["tenant_id"],
            token=_token(),
            active=False,
        )
        # Claimable document for tenant B — visible to the sweep enumerator.
        doc = InboxDocument(
            tenant_id=out["tenant_b"]["tenant_id"],
            vault_file_id=uuid.uuid4(),
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            filename="sweep-probe.jpg",
            mime="image/jpeg",
            size_bytes=1,
            source="EMAIL",
        )
        # A company owned by tenant B — for the coherence-trigger probe.
        cid_b = uuid.uuid4()
        session.add(
            Company(
                id=cid_b,
                tenant_id=out["tenant_b"]["tenant_id"],
                name=f"INBOX176-co-b-{suffix}",
                base_currency="AUD",
                fin_year_start_month=7,
            )
        )
        session.add_all([revoked, doc])
        await session.commit()
        out["tenant_b"]["revoked_token"] = revoked.token
        out["tenant_b"]["company_id"] = cid_b
        out["tenant_b"]["doc_id"] = doc.id
    yield out
    async with owner_seed_session() as session:
        for tbl in ("inbox_documents", _MSGS, _ADDR):
            for label in ("tenant_a", "tenant_b"):
                await session.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid").bindparams(
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
                pat=f"inbox176-%-{suffix}"
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 1. Catalog facts — ENABLE + FORCE + policy on BOTH tables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", [_ADDR, _MSGS])
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
    assert row.relrowsecurity is True, f"{table}: RLS not enabled"
    assert row.relforcerowsecurity is True, (
        f"{table}: FORCE ROW LEVEL SECURITY missing — the 0091 regression"
    )


@pytest.mark.parametrize("table", [_ADDR, _MSGS])
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
        addr_ids = {
            row.id for row in await conn.execute(text(f"SELECT id FROM {_ADDR}"))
        }
        msg_ids = {
            row.id for row in await conn.execute(text(f"SELECT id FROM {_MSGS}"))
        }
    assert a["addr_id"] in addr_ids
    assert b["addr_id"] not in addr_ids
    assert a["msg_id"] in msg_ids
    assert b["msg_id"] not in msg_ids


async def test_foreign_row_invisible_by_id(
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
                    f"SELECT id FROM {_ADDR} WHERE id = CAST(:i AS uuid)"
                ).bindparams(i=str(a["addr_id"]))
            )
        ).first()
    assert found is None, "cross-tenant address leaked through RLS by id probe"


@pytest.mark.parametrize("table", [_ADDR, _MSGS])
async def test_no_guc_sees_zero_rows(
    app_engine: Any, seeded: dict[str, Any], table: str
) -> None:
    async with app_engine.connect() as conn:
        count = (
            await conn.execute(text(f"SELECT count(*) FROM {table}"))
        ).scalar_one()
    assert count == 0, f"{table}: deny-by-default violated with no tenant GUC"


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
                    f"INSERT INTO {_ADDR} (tenant_id, token) "
                    "VALUES (CAST(:tid AS uuid), :tok)"
                ).bindparams(tid=str(b["tenant_id"]), tok=_token())
            )


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
                    f"INSERT INTO {_ADDR} (tenant_id, company_id, token) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), :tok)"
                ).bindparams(
                    tid=str(a["tenant_id"]),
                    cid=str(b["company_id"]),
                    tok=_token(),
                )
            )


# ---------------------------------------------------------------------------
# 5. Token is globally unique across tenants (despite RLS invisibility)
# ---------------------------------------------------------------------------


async def test_token_globally_unique_across_tenants(
    seeded: dict[str, Any],
) -> None:
    """Tenant A minting tenant B's token must hit the plain unique even
    though RLS hides B's row from A — the routing key is global."""
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with owner_seed_session() as session:
        session.add(
            InboxEmailAddress(tenant_id=a["tenant_id"], token=b["token"])
        )
        with pytest.raises(IntegrityError, match="uq_|token"):
            await session.flush()
        await session.rollback()


# ---------------------------------------------------------------------------
# 6-7. SECURITY DEFINER enumerators — cross-tenant reads for the app role
# ---------------------------------------------------------------------------


async def test_poll_enumerator_returns_all_active_addresses_no_guc(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """The 0084-posture routing map: the NOBYPASSRLS role, with NO GUC
    set (direct table scan returns zero rows), still enumerates every
    tenant's ACTIVE address — and not the revoked one."""
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT token, tenant_id FROM inbox_email_addresses_for_poll()")
            )
        ).all()
    by_token = {row.token: row.tenant_id for row in rows}
    assert by_token[a["token"]] == a["tenant_id"]
    assert by_token[b["token"]] == b["tenant_id"]
    assert b["revoked_token"] not in by_token, "revoked address must not route"


async def test_sweep_enumerator_returns_tenants_with_claimable_docs(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        tenant_ids = {
            row.tenant_id
            for row in await conn.execute(
                text("SELECT tenant_id FROM inbox_documents_tenants_for_sweep()")
            )
        }
    assert b["tenant_id"] in tenant_ids
