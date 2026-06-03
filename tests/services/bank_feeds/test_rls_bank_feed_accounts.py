"""RLS coverage for ``bank_feed_accounts`` and the other six tables 0085 closed.

What this file proves
---------------------
Migration 0085 closes RLS gaps on eight tables that escaped 0055 + 0083:

* Direct ``tenant_id`` policies (Class A): ``audit_log``,
  ``bsl_matches``, ``idempotency_records``.
* Indirect via ``companies`` FK (Class B): ``bank_feed_accounts``,
  ``bank_feed_clients``, ``ato_sbr_configs``, ``document_counters``,
  ``period_locks``.

For each class we prove:

1. ``relrowsecurity = t`` (RLS enabled).
2. ``relforcerowsecurity = t`` (table owner is also bound).
3. A ``tenant_isolation`` policy is installed.
4. A NOBYPASSRLS ``saebooks_app`` session with
   ``app.current_tenant`` set to tenant A reads only tenant A's rows
   from ``bank_feed_accounts``; tenant B's row is invisible across
   the boundary.
5. The 0084 SECURITY DEFINER enumerator still returns rows from both
   tenants — RLS additions on ``bank_feed_accounts`` must NOT break
   the cross-tenant enumeration that's the whole point of the
   function.

Test infra
----------
Reuses the role-flip pattern from ``test_rls_cli.py``:

* Owner engine = the default ``saebooks`` (BYPASSRLS) connection
  imported from ``saebooks.db.engine`` — used to seed two tenants
  + companies + feed accounts.
* App engine = a separate engine bound to ``saebooks_app`` with a
  test-only password — used to assert the policy is real.

Skip rule
---------
RLS is a Postgres feature and the CI matrix runs SQLite for fast
unit suites. We pytest-skip the whole module on non-Postgres backends.
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

# Pin SAEBOOKS_ENV BEFORE saebooks imports — same pattern as conftest.
os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import engine as _owner_engine
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant


# --------------------------------------------------------------------------- #
# saebooks_app engine — connects via the locked-down runtime role.            #
# --------------------------------------------------------------------------- #

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = (
    "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"
)


def _resolve_app_url() -> str:
    """saebooks_app DSN against the same DB the owner engine uses."""
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
            text(
                f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'"
            )
        )
    return True


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    ok = await _ensure_app_role_login()
    if not ok:
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(
        _resolve_app_url(), poolclass=NullPool, future=True
    )
    yield eng
    await eng.dispose()


# --------------------------------------------------------------------------- #
# Seed data: two tenants, each with a company + bank_feed_account.            #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    client_ids: dict[str, uuid.UUID] = {}

    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            ledger_account_id = uuid.uuid4()
            account_id = uuid.uuid4()
            client_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tid,
                    name=f"RLS85-{label}-{suffix}",
                    slug=f"rls85-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RLS85-{label}-{suffix}",
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
                    code=f"R85{suffix[:3]}-{label[-1]}",
                    name="RLS85 Bank",
                    account_type=AccountType.ASSET,
                )
            )
            await session.flush()
            session.add(
                BankFeedClient(
                    id=client_id,
                    company_id=cid,
                    sds_client_id=f"sds85-{suffix}-{label[-1]}",
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
                    sds_account_id=f"sds85-acct-{suffix}-{label[-1]}",
                    sds_institution_id="000000",
                    revoked_at=None,
                )
            )
            client_ids[label] = client_id
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "account_id": account_id,
            }
            await session.flush()
        await session.commit()

    yield out

    # Cleanup. companies CASCADE removes accounts + bank_feed_accounts +
    # bank_feed_clients via FK on delete cascade.
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text("DELETE FROM bank_feed_accounts WHERE id = :aid"),
                {"aid": row["account_id"]},
            )
        for cli_id in client_ids.values():
            await session.execute(
                text("DELETE FROM bank_feed_clients WHERE id = :cid"),
                {"cid": cli_id},
            )
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :cid"),
                {"cid": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": row["tenant_id"]},
            )
        await session.commit()


# --------------------------------------------------------------------------- #
# Structural assertions (cheap; no seeded data needed)                        #
# --------------------------------------------------------------------------- #


_GAP_TABLES = (
    "audit_log",
    "bsl_matches",
    "idempotency_records",
    "ato_sbr_configs",
    "bank_feed_accounts",
    "bank_feed_clients",
    "document_counters",
    "period_locks",
)


async def test_all_eight_tables_have_rls_enabled() -> None:
    """relrowsecurity + relforcerowsecurity both ``t`` for every table 0085 touches."""
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT relname, relrowsecurity, relforcerowsecurity
                    FROM pg_class
                    WHERE relname = ANY(:names)
                    ORDER BY relname
                    """
                ),
                {"names": list(_GAP_TABLES)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    missing = [t for t in _GAP_TABLES if t not in state]
    assert not missing, f"tables not present in pg_class: {missing}"
    bad = {t: v for t, v in state.items() if v != (True, True)}
    assert not bad, (
        f"RLS not fully enabled on {bad} — migration 0085 either failed "
        f"or was rolled back"
    )


async def test_all_eight_tables_have_tenant_isolation_policy() -> None:
    """Every table has exactly one ``tenant_isolation`` policy."""
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT tablename, policyname
                    FROM pg_policies
                    WHERE tablename = ANY(:names)
                      AND policyname = 'tenant_isolation'
                    ORDER BY tablename
                    """
                ),
                {"names": list(_GAP_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_GAP_TABLES) - have
    assert not missing, (
        f"tables missing tenant_isolation policy: {missing} — "
        f"migration 0085 incomplete"
    )


# --------------------------------------------------------------------------- #
# Behavioural assertions — the policy actually scopes rows                    #
# --------------------------------------------------------------------------- #


async def test_bank_feed_accounts_visible_to_own_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Tenant A's saebooks_app session can read tenant A's feed account."""
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    async with AppSession() as session:
        async with session.begin():
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
        f"tenant A could not see its own bank_feed_account "
        f"{a['account_id']} — RLS predicate is too tight"
    )


async def test_bank_feed_accounts_invisible_across_tenant(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Tenant A's session must not see tenant B's bank_feed_account row.

    This is the gap migration 0085 closed. Before 0085, this assertion
    would have FAILED (bank_feed_accounts had relrowsecurity=f and
    every tenant could see every other tenant's feed accounts).
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_tenant = seeded["tenant_a"]["tenant_id"]
    b_account = seeded["tenant_b"]["account_id"]

    async with AppSession() as session:
        async with session.begin():
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
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's bank_feed_account {b_account} — "
        f"the tenant_isolation policy on bank_feed_accounts is broken "
        f"or not FORCEd"
    )


async def test_bank_feed_accounts_full_table_scoped(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """Unbounded SELECT under tenant A returns A's row but not B's."""
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]

    async with AppSession() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(a["tenant_id"])},
            )
            ids = {
                row.id
                for row in (
                    await session.execute(
                        text("SELECT id FROM bank_feed_accounts")
                    )
                ).all()
            }
    assert a["account_id"] in ids, "own row missing from full-table SELECT"
    assert b["account_id"] not in ids, (
        "cross-tenant row leaked through SELECT * — bank_feed_accounts "
        "policy not scoping correctly"
    )


async def test_bank_feed_accounts_no_tenant_set_returns_zero(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """No ``app.current_tenant`` set => zero rows visible to saebooks_app.

    The current_setting() call returns NULL when missing_ok=true and
    the GUC is unset; ``company_id IN (SELECT id FROM companies WHERE
    tenant_id = NULL)`` is always false, so the table appears empty.
    This is the desired ``deny by default`` posture — exactly the same
    shape ``contacts`` etc. have under 0055.
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with AppSession() as session:
        async with session.begin():
            # No SET LOCAL — GUC is unset.
            rows = (
                await session.execute(
                    text("SELECT count(*) FROM bank_feed_accounts")
                )
            ).scalar_one()
    assert rows == 0, (
        f"expected 0 visible bank_feed_accounts with no tenant set, "
        f"got {rows} — RLS is not denying by default"
    )


# --------------------------------------------------------------------------- #
# 0084 SECDEF function must still work after 0085                              #
# --------------------------------------------------------------------------- #


async def test_secdef_enumerator_unaffected_by_rls_additions(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """``bank_feeds_active_accounts_for_sync()`` returns BOTH tenants' rows.

    The function is SECURITY DEFINER and owned by ``saebooks``
    (BYPASSRLS=t). RLS does not apply to BYPASSRLS roles regardless
    of FORCE on the underlying table. So the cross-tenant enumeration
    used by the sync-feeds CLI must still yield every tenant's active
    feed accounts after 0085 enables RLS on the underlying table.

    If THIS test fails after 0085, the migration broke the contract
    0084 was built around, and the CLI cannot run.
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    seeded_account_ids = {
        seeded["tenant_a"]["account_id"],
        seeded["tenant_b"]["account_id"],
    }
    async with AppSession() as session:
        # Deliberately NO SET LOCAL — the SECDEF path bypasses RLS by
        # design and must work without any GUC.
        rows = (
            await session.execute(
                text("SELECT * FROM bank_feeds_active_accounts_for_sync()")
            )
        ).all()
    found = {r.account_id for r in rows} & seeded_account_ids
    assert found == seeded_account_ids, (
        f"SECDEF enumerator no longer returns both seeded accounts after "
        f"0085. Expected {seeded_account_ids}; got intersect={found}. "
        f"This means migration 0085 broke 0084's contract — fix before "
        f"merging."
    )


async def test_secdef_enumerator_still_owned_by_saebooks() -> None:
    """The function is still SECURITY DEFINER + owned by saebooks.

    A migration that mutated the function's owner or stripped the
    SECDEF flag would silently break the CLI. Pin both attributes
    here so any regression surfaces in CI rather than at sync time.
    """
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT p.prosecdef, r.rolname AS owner
                    FROM pg_proc p
                    JOIN pg_roles r ON r.oid = p.proowner
                    WHERE p.proname = 'bank_feeds_active_accounts_for_sync'
                    """
                )
            )
        ).first()
    assert row is not None, "0084's enumerator function is missing"
    assert row.prosecdef is True, "function lost SECURITY DEFINER attribute"
    assert row.owner == "saebooks", (
        f"function owner changed to {row.owner!r} — must remain "
        f"'saebooks' (the BYPASSRLS role) for the SECDEF dodge to work"
    )


# --------------------------------------------------------------------------- #
# Class-A tables — direct tenant_id predicate, identical to 0055/0083 shape   #
# --------------------------------------------------------------------------- #


async def test_audit_log_class_a_predicate_uses_tenant_id() -> None:
    """audit_log policy is the standard ``tenant_id =`` shape."""
    async with _owner_engine.connect() as conn:
        qual = (
            await conn.execute(
                text(
                    """
                    SELECT qual FROM pg_policies
                    WHERE tablename = 'audit_log' AND policyname = 'tenant_isolation'
                    """
                )
            )
        ).scalar_one()
    assert "tenant_id" in qual, (
        f"expected tenant_id predicate on audit_log, got: {qual!r}"
    )
    assert "current_setting" in qual, (
        f"audit_log policy missing current_setting() — got: {qual!r}"
    )


async def test_bank_feed_accounts_class_b_predicate_uses_companies() -> None:
    """bank_feed_accounts policy joins through companies (no tenant_id col)."""
    async with _owner_engine.connect() as conn:
        qual = (
            await conn.execute(
                text(
                    """
                    SELECT qual FROM pg_policies
                    WHERE tablename = 'bank_feed_accounts'
                      AND policyname = 'tenant_isolation'
                    """
                )
            )
        ).scalar_one()
    assert "company_id" in qual, (
        f"expected company_id-based predicate on bank_feed_accounts, "
        f"got: {qual!r}"
    )
    assert "FROM companies" in qual, (
        f"bank_feed_accounts policy must subquery on companies, got: {qual!r}"
    )
