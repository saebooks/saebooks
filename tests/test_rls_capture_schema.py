"""RLS + search_path + FK coverage after the capture schema move (0173).

Migration 0173 relocates the four capture (bank-feed) tables
(``bank_feed_clients``/``bank_feed_accounts``/``bank_feed_issues``/
``bank_feed_external_creds``) from ``public`` into the ``capture`` schema
via ``ALTER TABLE ... SET SCHEMA``, extends ``search_path`` to
``public, preaccounting, capture`` on the database + both connecting
roles, and re-points the 0084 SECURITY DEFINER enumerator's pinned
search_path so it keeps resolving ``bank_feed_accounts`` in its new home.

This module proves — LIVE, against a migrated Postgres — the correctness
core of step 4 (runbook §§2.2/2.6/2.9):

1. All four capture tables now live in ``capture`` and are gone from
   ``public``.
2. The two Class-B tables (``bank_feed_clients`` / ``bank_feed_accounts``,
   company_id-routed per 0085) and the Class-A table
   (``bank_feed_external_creds``, tenant_id-routed per 0086) STILL carry
   ``relrowsecurity`` + ``relforcerowsecurity`` and a ``tenant_isolation``
   policy with its original predicate — the policy survived ``SET SCHEMA``.
   (``bank_feed_issues`` is an unscoped global cache with no RLS policy;
   we only assert it moved.)
3. A NOBYPASSRLS ``saebooks_app`` session:
   * resolves a moved table by its **unqualified** name
     (``FROM bank_feed_accounts``) — proving ``search_path`` picked up
     ``capture`` for the app role;
   * scoped to tenant A sees ONLY tenant A's rows across BOTH routing
     classes (Class-B ``bank_feed_accounts`` and Class-A
     ``bank_feed_external_creds``) — tenant B's rows are invisible;
   * with no ``app.current_tenant`` set, sees zero rows (deny by default).
4. The reverse fact FK ``bank_statement_lines.bank_feed_account_id ->
   bank_feed_accounts.id`` survived the move: the constraint now points at
   ``capture.bank_feed_accounts`` and a real cross-schema reference
   inserts and reads back.
5. The 0084 SECURITY DEFINER enumerator (``bank_feeds_active_accounts_for_
   sync()``) still returns every tenant's active feed account after the
   move — the capture-specific search_path fix works — and its pinned
   search_path now includes ``capture``.

Reuses the ``saebooks_app`` role-flip engine pattern from
``tests/test_rls_preaccounting_schema.py`` (0172) /
``tests/services/bank_feeds/test_rls_bank_feed_accounts.py`` (0085).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
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
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant
from tests.conftest import owner_seed_session

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"

# Every capture-owned table that 0173 moves.
_MOVED_TABLES = (
    "bank_feed_clients",
    "bank_feed_accounts",
    "bank_feed_issues",
    "bank_feed_external_creds",
)
# The RLS-scoped subset (bank_feed_issues is an unscoped global cache).
_CLASS_B_TABLES = ("bank_feed_clients", "bank_feed_accounts")  # company_id-routed
_CLASS_A_TABLES = ("bank_feed_external_creds",)  # tenant_id-routed
_SCOPED_TABLES = _CLASS_B_TABLES + _CLASS_A_TABLES


def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="RLS + schemas are Postgres features; meaningless on SQLite.",
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
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company + ledger account + feed client +
    feed account + external-cred row, plus (for tenant A) one
    bank_statement_line referencing the feed account across the schema
    boundary to prove the FK survived the move."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with owner_seed_session() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            ledger_account_id = uuid.uuid4()
            client_id = uuid.uuid4()
            account_id = uuid.uuid4()
            cred_id = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"CAP173-{label}-{suffix}",
                    slug=f"cap173-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"CAP173-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            session.add(
                Account(
                    id=ledger_account_id,
                    tenant_id=tid,
                    company_id=cid,
                    code=f"C173{suffix[:3]}-{label[-1]}",
                    name="CAP173 Bank",
                    account_type=AccountType.ASSET,
                )
            )
            await session.flush()
            session.add(
                BankFeedClient(
                    id=client_id,
                    company_id=cid,
                    sds_client_id=f"cap173-cli-{suffix}-{label[-1]}",
                    active=True,
                )
            )
            await session.flush()
            session.add(
                BankFeedAccount(
                    id=account_id,
                    company_id=cid,
                    bank_feed_client_id=client_id,
                    ledger_account_id=ledger_account_id,
                    sds_account_id=f"cap173-acct-{suffix}-{label[-1]}",
                    sds_institution_id="000000",
                    revoked_at=None,
                )
            )
            await session.flush()
            # bank_feed_external_creds has no ORM insert helper wired into
            # this test's imports; insert via the moved table directly.
            await session.execute(
                text(
                    "INSERT INTO capture.bank_feed_external_creds "
                    "(id, tenant_id, siss_client_id, status) "
                    "VALUES (:id, :tid, :cli, 'active')"
                ),
                {
                    "id": cred_id,
                    "tid": tid,
                    "cli": f"cap173-ext-{suffix}-{label[-1]}",
                },
            )
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "ledger_account_id": ledger_account_id,
                "client_id": client_id,
                "account_id": account_id,
                "cred_id": cred_id,
            }
            await session.flush()

        # Cross-schema FK exercise: a public bank_statement_lines row that
        # references tenant A's capture.bank_feed_accounts row. If the FK
        # did not survive SET SCHEMA this INSERT would raise.
        bsl_id = uuid.uuid4()
        a = out["tenant_a"]
        await session.execute(
            text(
                "INSERT INTO bank_statement_lines "
                "(id, company_id, tenant_id, account_id, txn_date, amount, "
                " status, bank_feed_account_id, version) "
                "VALUES (:id, :cid, :tid, :acc, :d, :amt, 'UNMATCHED', "
                " :bfa, 1)"
            ),
            {
                "id": bsl_id,
                "cid": a["company_id"],
                "tid": a["tenant_id"],
                "acc": a["ledger_account_id"],
                "d": date(2026, 7, 1),
                "amt": Decimal("12.34"),
                "bfa": a["account_id"],
            },
        )
        out["bsl_id"] = bsl_id
        await session.commit()
    yield out

    async with owner_seed_session() as session:
        await session.execute(
            text("DELETE FROM bank_statement_lines WHERE id = :i"),
            {"i": out["bsl_id"]},
        )
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text(
                    "DELETE FROM capture.bank_feed_external_creds WHERE id = :i"
                ),
                {"i": row["cred_id"]},
            )
            await session.execute(
                text("DELETE FROM capture.bank_feed_accounts WHERE id = :i"),
                {"i": row["account_id"]},
            )
            await session.execute(
                text("DELETE FROM capture.bank_feed_clients WHERE id = :i"),
                {"i": row["client_id"]},
            )
            await session.execute(
                text("DELETE FROM accounts WHERE id = :i"),
                {"i": row["ledger_account_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :i"),
                {"i": row["tenant_id"]},
            )
        await session.commit()


# --------------------------------------------------------------------------- #
# Structural — tables moved                                                    #
# --------------------------------------------------------------------------- #


async def test_moved_tables_live_in_capture_schema() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'capture' AND tablename = ANY(:names)"
                ),
                {"names": list(_MOVED_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_MOVED_TABLES) - have
    assert not missing, f"tables not in capture schema after 0173: {missing}"

    async with _owner_engine.connect() as conn:
        pub = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(_MOVED_TABLES)},
            )
        ).all()
    left_behind = {r.tablename for r in pub}
    assert not left_behind, (
        f"tables still present in public after SET SCHEMA: {left_behind}"
    )


# --------------------------------------------------------------------------- #
# Structural — RLS survived the move on the scoped tables                      #
# --------------------------------------------------------------------------- #


async def test_scoped_tables_keep_force_rls_after_move() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'capture' AND c.relname = ANY(:names) "
                    "ORDER BY c.relname"
                ),
                {"names": list(_SCOPED_TABLES)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    missing = [t for t in _SCOPED_TABLES if t not in state]
    assert not missing, f"scoped tables not found in capture: {missing}"
    bad = {t: v for t, v in state.items() if v != (True, True)}
    assert not bad, f"RLS not fully enabled after SET SCHEMA on {bad}"


async def test_scoped_tables_keep_tenant_isolation_policy_after_move() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, schemaname, qual FROM pg_policies "
                    "WHERE tablename = ANY(:names) "
                    "AND policyname = 'tenant_isolation' ORDER BY tablename"
                ),
                {"names": list(_SCOPED_TABLES)},
            )
        ).all()
    have = {r.tablename: r for r in rows}
    missing = set(_SCOPED_TABLES) - set(have)
    assert not missing, (
        f"tenant_isolation policy lost on {missing} after SET SCHEMA"
    )
    for r in rows:
        assert r.schemaname == "capture", (
            f"{r.tablename} policy did not follow the table into capture "
            f"(schemaname={r.schemaname})"
        )
        assert "current_setting" in r.qual, (
            f"{r.tablename} policy predicate lost its GUC ref: {r.qual!r}"
        )
    # Class B routes through companies; Class A keys tenant_id directly.
    for t in _CLASS_B_TABLES:
        q = have[t].qual
        assert "company_id" in q and "companies" in q, (
            f"{t} Class-B predicate changed: {q!r}"
        )
    for t in _CLASS_A_TABLES:
        q = have[t].qual
        assert "tenant_id" in q, f"{t} Class-A predicate changed: {q!r}"


# --------------------------------------------------------------------------- #
# Behavioural — search_path + RLS through the runtime app role                 #
# --------------------------------------------------------------------------- #


async def test_app_role_resolves_unqualified_moved_table(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Unqualified ``FROM bank_feed_accounts`` must resolve for saebooks_app
    — proving search_path picked up ``capture`` for the RLS-bound role."""
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM bank_feed_accounts WHERE id = :aid"),
                {"aid": a["account_id"]},
            )
        ).all()
    assert len(visible) == 1, (
        "tenant A could not see its own bank_feed_account via unqualified "
        "name — search_path did not include capture, or RLS too tight"
    )


async def test_class_b_invisible_across_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_tenant = seeded["tenant_a"]["tenant_id"]
    b_account = seeded["tenant_b"]["account_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM bank_feed_accounts WHERE id = :aid"),
                {"aid": b_account},
            )
        ).all()
    assert visible == [], (
        "CROSS-TENANT LEAK: tenant A saw tenant B's bank_feed_account "
        "through moved capture.bank_feed_accounts — Class-B RLS did not "
        "survive SET SCHEMA"
    )


async def test_class_a_invisible_across_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_tenant = seeded["tenant_a"]["tenant_id"]
    b_cred = seeded["tenant_b"]["cred_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text(
                    "SELECT id FROM bank_feed_external_creds WHERE id = :cid"
                ),
                {"cid": b_cred},
            )
        ).all()
    assert visible == [], (
        "CROSS-TENANT LEAK: tenant A saw tenant B's bank_feed_external_cred "
        "through moved capture.bank_feed_external_creds — Class-A RLS did "
        "not survive SET SCHEMA"
    )


async def test_no_tenant_set_denies_by_default(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    async with AppSession() as session, session.begin():
        # Deliberately do NOT set app.current_tenant.
        acct = (
            await session.execute(
                text("SELECT id FROM bank_feed_accounts WHERE id = :i"),
                {"i": a["account_id"]},
            )
        ).all()
        cred = (
            await session.execute(
                text("SELECT id FROM bank_feed_external_creds WHERE id = :i"),
                {"i": a["cred_id"]},
            )
        ).all()
    assert acct == [] and cred == [], (
        "rows visible with no app.current_tenant set — deny-by-default "
        "broke after the schema move"
    )


# --------------------------------------------------------------------------- #
# FK survival — bank_statement_lines.bank_feed_account_id -> capture           #
# --------------------------------------------------------------------------- #


async def test_reverse_fact_fk_repointed_into_capture() -> None:
    """The inbound fact FK now references capture.bank_feed_accounts."""
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT nf.nspname AS ref_schema, cf.relname AS ref_table
                    FROM pg_constraint con
                    JOIN pg_class cs ON cs.oid = con.conrelid
                    JOIN pg_class cf ON cf.oid = con.confrelid
                    JOIN pg_namespace nf ON nf.oid = cf.relnamespace
                    WHERE con.contype = 'f'
                      AND cs.relname = 'bank_statement_lines'
                      AND cf.relname = 'bank_feed_accounts'
                    """
                )
            )
        ).first()
    assert row is not None, (
        "FK bank_statement_lines.bank_feed_account_id -> bank_feed_accounts "
        "vanished after SET SCHEMA"
    )
    assert row.ref_schema == "capture", (
        f"FK re-pointed to the wrong schema: {row.ref_schema} "
        "(expected capture)"
    )


async def test_reverse_fact_fk_enforced_across_schema(
    seeded: dict[str, Any]
) -> None:
    """The seeded bank_statement_line references a capture feed account and
    persisted — a live cross-schema FK reference."""
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT bank_feed_account_id FROM bank_statement_lines "
                    "WHERE id = :i"
                ),
                {"i": seeded["bsl_id"]},
            )
        ).first()
    assert row is not None, "seeded bank_statement_line missing"
    assert row.bank_feed_account_id == seeded["tenant_a"]["account_id"], (
        "cross-schema FK reference did not persist — "
        "bank_statement_lines -> capture.bank_feed_accounts is broken"
    )


# --------------------------------------------------------------------------- #
# SECDEF enumerator — the capture-specific search_path fix                     #
# --------------------------------------------------------------------------- #


async def test_secdef_function_search_path_includes_capture() -> None:
    """0084's function must pin a search_path that reaches ``capture`` or
    sync-feeds cannot resolve the moved bank_feed_accounts."""
    async with _owner_engine.connect() as conn:
        cfg = (
            await conn.execute(
                text(
                    "SELECT proconfig FROM pg_proc "
                    "WHERE proname = 'bank_feeds_active_accounts_for_sync'"
                )
            )
        ).scalar_one()
    joined = " ".join(cfg or [])
    assert "capture" in joined, (
        "SECDEF enumerator search_path was not widened to include capture — "
        f"proconfig={cfg!r}; sync-feeds will fail post-move"
    )


async def test_secdef_enumerator_resolves_after_move(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """``bank_feeds_active_accounts_for_sync()`` still returns BOTH tenants'
    active accounts — proving the function resolves capture.bank_feed_accounts
    through its widened pinned search_path (and RLS is bypassed as designed)."""
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    seeded_ids = {
        seeded["tenant_a"]["account_id"],
        seeded["tenant_b"]["account_id"],
    }
    async with AppSession() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM bank_feeds_active_accounts_for_sync()")
            )
        ).all()
    found = {r.account_id for r in rows} & seeded_ids
    assert found == seeded_ids, (
        f"SECDEF enumerator lost seeded accounts after 0173. Expected "
        f"{seeded_ids}; intersect={found}. The function's pinned search_path "
        f"probably still omits capture."
    )
