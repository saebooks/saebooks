"""Phase 3a ic_outbox / ic_inbox — FORCE-RLS + cross-tenant probe + edge columns.

Covers migration 0159 and the new ORM models. Mirrors the 0154/0156 patterns:

Structural (owner-role catalog inspection, no seeding):
  * ENABLE + FORCE RLS + a ``tenant_isolation`` policy on ``ic_outbox`` and
    ``ic_inbox``;
  * the new REMOTE columns exist on ``ic_edges`` and ``partner_company_id`` is
    now nullable;
  * the idempotency / replay unique constraints exist.

Behavioural cross-tenant probe (NOBYPASSRLS saebooks_app role):
  * tenant A sees its own ``ic_outbox`` row; tenant B's row is invisible across
    the boundary; with no tenant set, zero rows (deny by default). Same probe on
    ``ic_inbox``.

These are the standing new-table RLS checklist tests — Postgres only.
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

from saebooks.db import engine as _owner_engine
from saebooks.models.company import Company
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcInbox,
    IcInboxStatus,
    IcOutbox,
    IcOutboxStatus,
    IcTxn,
    IcTxnStatus,
)
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only

_NEW_TABLES = ("ic_outbox", "ic_inbox")
_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# --------------------------------------------------------------------------- #
# Structural RLS assertions
# --------------------------------------------------------------------------- #
async def test_new_tables_have_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names) ORDER BY relname"
                ),
                {"names": list(_NEW_TABLES)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    missing = [t for t in _NEW_TABLES if t not in state]
    assert not missing, f"relay tables absent from pg_class: {missing}"
    bad = {t: v for t, v in state.items() if v != (True, True)}
    assert not bad, f"RLS not ENABLE+FORCE on {bad} — migration 0159 incomplete"


async def test_new_tables_have_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, qual FROM pg_policies "
                    "WHERE tablename = ANY(:names) "
                    "AND policyname = 'tenant_isolation' ORDER BY tablename"
                ),
                {"names": list(_NEW_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_NEW_TABLES) - have
    assert not missing, f"relay tables missing tenant_isolation policy: {missing}"
    for r in rows:
        assert "tenant_id" in r.qual and "current_setting" in r.qual, (
            f"{r.tablename} policy is not the standard tenant predicate: {r.qual!r}"
        )


async def test_ic_edges_has_remote_columns() -> None:
    expected = {
        "topology",
        "partner_tenant_id",
        "partner_endpoint",
        "relay_pubkey",
        "relay_privkey_ciphertext",
        "relay_token_prefix",
        "relay_token_hash",
        "relay_status",
        "authorised_by_principal_id",
    }
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'ic_edges'"
                )
            )
        ).all()
    cols = {r.column_name: r.is_nullable for r in rows}
    missing = expected - set(cols)
    assert not missing, f"ic_edges missing REMOTE columns: {missing}"
    # partner_company_id relaxed to nullable for REMOTE edges.
    assert cols.get("partner_company_id") == "YES", (
        "partner_company_id should be nullable after 0159 (REMOTE has no local "
        f"partner) — got is_nullable={cols.get('partner_company_id')!r}"
    )


async def test_idempotency_and_replay_constraints_exist() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conname IN ("
                    "'uq_ic_outbox_tenant_idempotency_key',"
                    "'uq_ic_inbox_tenant_ic_txn_id',"
                    "'uq_ic_inbox_tenant_nonce')"
                )
            )
        ).all()
    names = {r.conname for r in rows}
    for c in (
        "uq_ic_outbox_tenant_idempotency_key",
        "uq_ic_inbox_tenant_ic_txn_id",
        "uq_ic_inbox_tenant_nonce",
    ):
        assert c in names, f"missing constraint {c} — idempotency/replay guard absent"


# --------------------------------------------------------------------------- #
# Cross-tenant probe via the NOBYPASSRLS saebooks_app role
# --------------------------------------------------------------------------- #
def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


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
async def seeded_relay_rows() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company + account + REMOTE ic_edge + outbox/inbox.

    The REMOTE edge carries a NULL ``partner_company_id`` (proving the 0159
    relax) and a real control account (the 0154 composite FK still binds it to
    THIS company's CoA — REMOTE only relaxes the partner side, not the control).
    """
    from saebooks.models.account import Account, AccountType

    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            acct_id = uuid.uuid4()
            txn_id = uuid.uuid4()
            edge_id = uuid.uuid4()
            outbox_id = uuid.uuid4()
            inbox_id = uuid.uuid4()
            ext_txn_id = uuid.uuid4()
            session.add(
                Tenant(id=tid, name=f"IC159-{label}-{suffix}",
                       slug=f"ic159-{label}-{suffix}")
            )
            await session.flush()
            session.add(
                Company(id=cid, tenant_id=tid,
                        name=f"IC159-{label}-{suffix}", base_currency="AUD")
            )
            await session.flush()
            # Real control account FIRST (the composite FK requires it).
            session.add(
                Account(id=acct_id, tenant_id=tid, company_id=cid,
                        code=f"2-22{suffix[:2]}{label[-1]}", name="Directors Loan",
                        account_type=AccountType.LIABILITY)
            )
            await session.flush()
            session.add(
                IcTxn(id=txn_id, tenant_id=tid, company_id=cid,
                      description=f"probe-{label}", status=IcTxnStatus.ACTIVE)
            )
            # REMOTE edge: NULL partner_company_id, real control account.
            session.add(
                IcEdge(id=edge_id, tenant_id=tid, company_id=cid,
                       partner_company_id=None, control_account_id=acct_id,
                       direction=IcEdgeDirection.ORIGINATOR)
            )
            await session.flush()
            session.add(
                IcOutbox(id=outbox_id, tenant_id=tid, company_id=cid,
                         ic_txn_id=txn_id, edge_id=edge_id, idempotency_key=txn_id,
                         nonce=uuid.uuid4(), payload_json={"amount": "5000.00"},
                         signature=b"\x00" * 64, status=IcOutboxStatus.PENDING)
            )
            session.add(
                IcInbox(id=inbox_id, tenant_id=tid, company_id=cid,
                        ic_txn_id=ext_txn_id, edge_id=edge_id, nonce=uuid.uuid4(),
                        payload_json={"amount": "5000.00"},
                        signature=b"\x00" * 64, status=IcInboxStatus.RECEIVED)
            )
            await session.flush()
            out[label] = {
                "tenant_id": tid, "company_id": cid, "account_id": acct_id,
                "txn_id": txn_id, "edge_id": edge_id, "outbox_id": outbox_id,
                "inbox_id": inbox_id, "ext_txn_id": ext_txn_id,
            }
        await session.commit()

    yield out

    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            d = out[label]
            await session.execute(text("DELETE FROM ic_inbox WHERE id = :i"),
                                  {"i": d["inbox_id"]})
            await session.execute(text("DELETE FROM ic_outbox WHERE id = :i"),
                                  {"i": d["outbox_id"]})
            await session.execute(text("DELETE FROM ic_edges WHERE id = :i"),
                                  {"i": d["edge_id"]})
            await session.execute(text("DELETE FROM ic_txn WHERE id = :i"),
                                  {"i": d["txn_id"]})
            await session.execute(text("DELETE FROM accounts WHERE id = :i"),
                                  {"i": d["account_id"]})
            await session.execute(text("DELETE FROM companies WHERE id = :i"),
                                  {"i": d["company_id"]})
            await session.execute(text("DELETE FROM tenants WHERE id = :i"),
                                  {"i": d["tenant_id"]})
        await session.commit()


@pytest.mark.parametrize("table", ["ic_outbox", "ic_inbox"])
async def test_relay_row_visible_to_own_tenant(
    app_engine: Any, seeded_relay_rows: dict[str, Any], table: str
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded_relay_rows["tenant_a"]
    row_id = a["outbox_id"] if table == "ic_outbox" else a["inbox_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text(f"SELECT id FROM {table} WHERE id = :i"), {"i": row_id}
            )
        ).all()
    assert len(visible) == 1, f"tenant A cannot see its own {table} row — RLS too tight"


@pytest.mark.parametrize("table", ["ic_outbox", "ic_inbox"])
async def test_relay_row_invisible_across_tenant(
    app_engine: Any, seeded_relay_rows: dict[str, Any], table: str
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_relay_rows["tenant_a"]["tenant_id"]
    b = seeded_relay_rows["tenant_b"]
    b_row = b["outbox_id"] if table == "ic_outbox" else b["inbox_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text(f"SELECT id FROM {table} WHERE id = :i"), {"i": b_row}
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's {table} {b_row} — tenant_isolation "
        f"broken/not FORCEd on {table}"
    )


@pytest.mark.parametrize("table", ["ic_outbox", "ic_inbox"])
async def test_relay_no_tenant_set_returns_zero(app_engine: Any, table: str) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (
            await session.execute(text(f"SELECT count(*) FROM {table}"))
        ).scalar_one()
    assert rows == 0, (
        f"expected 0 {table} with no tenant set, got {rows} — not deny-by-default"
    )


async def test_cross_tenant_write_check_blocks_foreign_insert(
    app_engine: Any, seeded_relay_rows: dict[str, Any]
) -> None:
    """WITH CHECK must reject inserting an ic_outbox row for a FOREIGN tenant.

    Tenant A's session, bound to tenant A, tries to insert a row stamped with
    tenant B's tenant_id. FORCE-RLS WITH CHECK must refuse it.
    """
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded_relay_rows["tenant_a"]
    b = seeded_relay_rows["tenant_b"]
    blew_up = False
    async with AppSession() as session:
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.current_tenant', :t, true)"),
                    {"t": str(a["tenant_id"])},
                )
                await session.execute(
                    text(
                        "INSERT INTO ic_outbox "
                        "(id, tenant_id, company_id, ic_txn_id, edge_id, "
                        " idempotency_key, nonce, payload_json, signature, "
                        " status, attempts, issued_at, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :tb, :cb, :txn, :edge, "
                        " gen_random_uuid(), gen_random_uuid(), '{}'::jsonb, "
                        " '\\x00', 'PENDING', 0, now(), now(), now())"
                    ),
                    {
                        "tb": str(b["tenant_id"]),  # FOREIGN tenant
                        "cb": str(b["company_id"]),
                        "txn": str(b["txn_id"]),
                        "edge": str(b["edge_id"]),
                    },
                )
        except Exception:
            blew_up = True
    assert blew_up, (
        "tenant A inserted an ic_outbox row for tenant B — WITH CHECK on the "
        "tenant_isolation policy is missing or not FORCEd"
    )
