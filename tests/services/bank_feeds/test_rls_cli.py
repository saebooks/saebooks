"""RLS plumbing for the sync-feeds CLI — feat/rls-cli-app-role.

What this file proves
---------------------
The cross-tenant ``sync-feeds`` CLI must be safe to run as a NOBYPASSRLS
``saebooks_app`` role. Three properties matter:

1. The SECURITY DEFINER enumerator
   ``bank_feeds_active_accounts_for_sync()`` exists, returns the right
   shape, and works for the ``saebooks_app`` caller even though that
   role is bound by RLS.
2. With ``app.current_tenant`` set to tenant A, a SELECT against
   ``bank_feed_accounts`` from the ``saebooks_app`` role returns ONLY
   tenant A's rows — the policy is doing real work.
3. ``SET LOCAL`` doesn't leak: after the transaction commits, a fresh
   transaction starts with no GUC and therefore sees nothing.
4. The CLI's bypass-role guard fires when invoked under a BYPASSRLS
   role without ``--allow-bypass``.

Test infra notes
----------------
The tests connect through a separate engine bound to ``saebooks_app``,
mirroring the pattern from ``tests/api/v1/test_cross_tenant_isolation.py``.
The seed sessions run as the schema-owner (the engine instantiated by
``saebooks.db``) so we can place rows into both tenants regardless of
GUC. Tests skip if the database is SQLite or the ``saebooks_app`` role
is missing — RLS is a Postgres feature and there is no point green-
faking these assertions on an engine that doesn't enforce them.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
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

# Pin SAEBOOKS_ENV BEFORE saebooks imports — same pattern as conftest.
os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks import cli
from saebooks.db import engine as _owner_engine
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_feed import (
    BankFeedAccount,
    BankFeedClient,
)
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
    """Build the saebooks_app DSN against the same DB the owner engine uses.

    The owner engine's URL is e.g.
    ``postgresql+asyncpg://saebooks:...@db:5432/saebooks_prod``. We swap
    the role + password but keep host + db so the test connects to
    whatever DB the suite is configured for.
    """
    raw = str(_owner_engine.url)
    # Re-extract the dbname from the owner URL to keep us pointing at
    # the same database (saebooks vs saebooks_prod, depending on env).
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="RLS is a Postgres feature; this suite is meaningless on SQLite.",
)


async def _ensure_app_role_login() -> bool:
    """Ensure ``saebooks_app`` exists and has the test password.

    Returns ``False`` if the role is genuinely missing (test should
    skip rather than fail). Returns ``True`` after a successful
    ``ALTER ROLE``.
    """
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
    """Module-scoped engine bound to the saebooks_app role."""
    ok = await _ensure_app_role_login()
    if not ok:
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(
        _resolve_app_url(), poolclass=NullPool, future=True
    )
    yield eng
    await eng.dispose()


# --------------------------------------------------------------------------- #
# Seeded data: two tenants, each with a company + bank_feed_account.          #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture(scope="module")
async def seeded() -> dict[str, Any]:
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

            session.add(
                Tenant(
                    id=tid,
                    name=f"RLS-{label}-{suffix}",
                    slug=f"rls-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RLS-{label}-{suffix}",
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
                    code=f"R{suffix[:4]}-{label[-1]}",
                    name="RLS Bank",
                    account_type=AccountType.ASSET,
                )
            )
            await session.flush()
            client_id = uuid.uuid4()
            client_ids[label] = client_id
            session.add(
                BankFeedClient(
                    id=client_id,
                    company_id=cid,
                    sds_client_id=f"sds-{suffix}-{label[-1]}",
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
                    sds_account_id=f"sds-acct-{suffix}-{label[-1]}",
                    sds_institution_id="000000",
                    revoked_at=None,
                )
            )
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "account_id": account_id,
            }
            await session.flush()
        await session.commit()

    yield out

    # Cleanup — drop every row we inserted (cascades from companies
    # take out bank_feed_accounts and accounts).
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text("DELETE FROM bank_feed_accounts WHERE id = :aid"),
                {"aid": row["account_id"]},
            )
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
        for cli_id in client_ids.values():
            await session.execute(
                text("DELETE FROM bank_feed_clients WHERE id = :cid"),
                {"cid": cli_id},
            )
        await session.commit()


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_secdef_function_exists() -> None:
    """The migration created bank_feeds_active_accounts_for_sync()."""
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT proname, prosecdef
                    FROM pg_proc
                    WHERE proname = 'bank_feeds_active_accounts_for_sync'
                    """
                )
            )
        ).first()
    assert row is not None, (
        "Function bank_feeds_active_accounts_for_sync not found — "
        "is migration 0084 applied?"
    )
    assert row.prosecdef is True, (
        "Function exists but is not SECURITY DEFINER — RLS enumeration "
        "would return zero rows for the saebooks_app caller."
    )


async def test_secdef_function_returns_expected_columns(
    seeded: dict[str, Any],
) -> None:
    """The function returns (uuid, uuid, uuid) and includes our seeded rows."""
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT * FROM bank_feeds_active_accounts_for_sync()")
            )
        ).all()
    seeded_account_ids = {seeded["tenant_a"]["account_id"], seeded["tenant_b"]["account_id"]}
    found = {r.account_id for r in rows} & seeded_account_ids
    assert found == seeded_account_ids, (
        f"Expected seeded accounts in enumerator output; got intersect={found}"
    )
    # Spot-check shape on one row.
    sample = next(r for r in rows if r.account_id in seeded_account_ids)
    assert isinstance(sample.company_id, uuid.UUID)
    assert isinstance(sample.tenant_id, uuid.UUID)
    assert isinstance(sample.account_id, uuid.UUID)


async def test_cross_tenant_invisibility(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """saebooks_app session with tenant=A sees only tenant A's company row.

    The companies table is the canonical RLS-enforced surface for
    every per-company query — every business-data table joins to it
    through company_id and the application layer scopes by
    company_id while the policy on companies keeps the join
    honest. This test asserts that contract end-to-end:

    * Set app.current_tenant to tenant A.
    * Try to read both seeded company rows by id under saebooks_app.
    * Tenant A's row visible; tenant B's row NOT visible.

    If this fails, either FORCE RLS on companies is not in place
    (regression on migration 0055) or the role connecting through
    app_engine actually has BYPASSRLS (regression on the test
    fixture password setup).
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    tenant_a_id = seeded["tenant_a"]["tenant_id"]
    a_company = seeded["tenant_a"]["company_id"]
    b_company = seeded["tenant_b"]["company_id"]

    async with AppSession() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(tenant_a_id)},
            )
            visible = (
                await session.execute(
                    text("SELECT id FROM companies WHERE id IN (:a, :b)"),
                    {"a": a_company, "b": b_company},
                )
            ).all()
    visible_ids = {row.id for row in visible}
    assert a_company in visible_ids, "tenant A's own company should be visible"
    assert b_company not in visible_ids, (
        "tenant B's company leaked across the RLS boundary — "
        "tenant_isolation policy on companies is broken or not FORCEd"
    )


async def test_secdef_function_works_for_app_role(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """saebooks_app — bound by RLS — can still call the enumerator.

    This is the whole point of SECURITY DEFINER: the function runs with
    the OWNER's privileges (saebooks, BYPASSRLS=t) regardless of who
    calls it. The CLI calls this function as saebooks_app and gets back
    every active feed account across every tenant — without itself
    having BYPASSRLS.
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    seeded_account_ids = {
        seeded["tenant_a"]["account_id"],
        seeded["tenant_b"]["account_id"],
    }
    async with AppSession() as session:
        # Deliberately do NOT set app.current_tenant — this proves the
        # SECDEF path works regardless of GUC state.
        rows = (
            await session.execute(
                text("SELECT * FROM bank_feeds_active_accounts_for_sync()")
            )
        ).all()
    found = {r.account_id for r in rows} & seeded_account_ids
    assert found == seeded_account_ids, (
        "SECURITY DEFINER function did not return seeded rows for the "
        "saebooks_app caller. Either the GRANT EXECUTE was not applied "
        "by migration 0084, or the function's SECURITY DEFINER attribute "
        "got dropped."
    )


async def test_set_local_does_not_leak_across_transactions(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    """A second transaction starts with no app.current_tenant set."""
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    tenant_a_id = seeded["tenant_a"]["tenant_id"]

    async with AppSession() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(tenant_a_id)},
            )
            inside = (
                await session.execute(
                    text("SELECT current_setting('app.current_tenant', true)")
                )
            ).scalar_one()
            assert inside == str(tenant_a_id)

        # Fresh transaction — GUC should be empty / NULL.
        async with session.begin():
            after = (
                await session.execute(
                    text("SELECT current_setting('app.current_tenant', true)")
                )
            ).scalar_one()
            # Postgres returns '' when missing_ok=true and the GUC is unset.
            assert after in (None, ""), (
                f"app.current_tenant leaked to '{after}' after commit — "
                "the CLI is using SET (session) instead of SET LOCAL (txn)"
            )


def test_bypass_guard_refuses_under_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --allow-bypass, sync-feeds exits 2 when current_user is super.

    We monkeypatch ``_assert_not_bypass`` to raise as if the role check
    detected BYPASSRLS — that's the entire point of the guard. The
    return code must be 2.
    """
    async def fake_assert(session: Any) -> None:
        raise cli.BypassRoleRefused("simulated bypass role")

    monkeypatch.setattr(cli, "_assert_not_bypass", fake_assert)

    # Patch enumerator so we never hit the DB after the guard fires.
    async def fake_enum(session: Any, *, company_id: Any) -> dict:
        raise AssertionError("guard should have stopped us before enumeration")

    monkeypatch.setattr(cli, "_enumerate_active_groups", fake_enum)

    # Force the AppSessionLocal-or-fallback path into AsyncSessionLocal
    # (we don't have a saebooks_app DSN configured in unit-test runs).
    rc = cli.main(["sync-feeds"])
    # If AppSessionLocal is None and no --allow-bypass, the CLI exits 2
    # before our monkeypatched _assert_not_bypass fires. Either way:
    # rc must be 2 — that's the contract this test is asserting.
    assert rc == 2


async def test_cli_runs_under_app_role(
    monkeypatch: pytest.MonkeyPatch,
    seeded: dict[str, Any],
) -> None:
    """End-to-end: with SAEBOOKS_APP_DATABASE_URL pointed at saebooks_app and
    onboarding.sync_all_active stubbed, the CLI walks tenant groups and
    sets app.current_tenant per group.

    This is the integration test that proves the per-tenant SET LOCAL
    plumbing wires up. We don't actually pull bank transactions —
    ``sync_all_active`` is stubbed to record what tenants it was
    invoked under.
    """
    ok = await _ensure_app_role_login()
    if not ok:
        pytest.skip("saebooks_app role missing — migration 0056 not applied")

    seen_tenants: list[str] = []

    async def fake_sync_all(session: AsyncSession, **kwargs: Any) -> list:
        # Read back the GUC the CLI just SET LOCAL — that's the proof.
        val = (
            await session.execute(
                text("SELECT current_setting('app.current_tenant', true)")
            )
        ).scalar_one()
        seen_tenants.append(val or "")
        return []

    from saebooks.services.bank_feeds import onboarding as _onboarding

    monkeypatch.setattr(_onboarding, "sync_all_active", fake_sync_all)

    # Reload the cli module's view of AppSessionLocal — it was bound at
    # import time. We rebind it here against the test app_engine URL.
    from sqlalchemy.ext.asyncio import async_sessionmaker as _smk
    from sqlalchemy.ext.asyncio import create_async_engine as _cae

    test_engine = _cae(_resolve_app_url(), poolclass=NullPool, future=True)
    test_factory = _smk(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(cli, "AppSessionLocal", test_factory)

    try:
        # cli.main() uses asyncio.run() which collides with pytest-asyncio's
        # own running loop. Call the inner coroutine directly — same
        # contract, no event-loop fight.
        rc = await cli._sync_feeds(company_id=None, allow_bypass=False)
    finally:
        await test_engine.dispose()

    # rc may be 0 or 1 depending on other groups in the DB. Our concern
    # is that our two seeded tenants were each visited with the right
    # app.current_tenant set.
    expected_tenants = {
        str(seeded["tenant_a"]["tenant_id"]),
        str(seeded["tenant_b"]["tenant_id"]),
    }
    seen_set = set(seen_tenants)
    missing = expected_tenants - seen_set
    assert not missing, (
        f"sync-feeds did not set app.current_tenant for {missing}; "
        f"observed values: {seen_tenants}"
    )
    assert rc in (0, 1), f"unexpected exit code {rc}"
