"""Intercompany Phase 1 — LOCAL two-leg posting + RLS coverage.

Covers migration 0154 (``ic_txn`` / ``ic_edges`` / ``ic_legs``) and
``services/intercompany.py``:

Structural / RLS (Postgres only):
  * RLS ENABLE + FORCE + a ``tenant_isolation`` policy on all three tables.
  * Cross-tenant probe: a NOBYPASSRLS ``saebooks_app`` session scoped to tenant
    A cannot read tenant B's ``ic_txn`` row; with no tenant set, zero rows
    (deny by default).

Service (LOCAL same-tenant pair, via the superuser AsyncSessionLocal — mirrors
the existing tests/test_cross_company_fk.py pattern):
  * Happy path: both legs post, balanced, ``origin=INTERCOMPANY``, two
    ``ic_legs`` link to one ACTIVE ``ic_txn``; directors-loan sign convention.
  * Atomicity: a failing counterparty leg rolls the WHOLE pair back — no JE, no
    ic_txn, no ic_legs persist.
  * Reversal: both legs reverse, ``ic_txn`` -> REVERSED, reversals linked.
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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcLeg,
    IcLegSide,
    IcTxn,
    IcTxnStatus,
)
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, JournalOrigin
from saebooks.models.tenant import Tenant
from saebooks.services import intercompany as ic_svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TABLES = ("ic_txn", "ic_edges", "ic_legs")

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# --------------------------------------------------------------------------- #
# Structural RLS assertions (no seeding needed)
# --------------------------------------------------------------------------- #
async def test_ic_tables_have_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names) ORDER BY relname"
                ),
                {"names": list(_TABLES)},
            )
        ).all()
    state = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    missing = [t for t in _TABLES if t not in state]
    assert not missing, f"ic tables absent from pg_class: {missing}"
    bad = {t: v for t, v in state.items() if v != (True, True)}
    assert not bad, f"RLS not ENABLE+FORCE on {bad} — migration 0154 incomplete"


async def test_ic_tables_have_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, qual FROM pg_policies "
                    "WHERE tablename = ANY(:names) "
                    "AND policyname = 'tenant_isolation' ORDER BY tablename"
                ),
                {"names": list(_TABLES)},
            )
        ).all()
    have = {r.tablename for r in rows}
    missing = set(_TABLES) - have
    assert not missing, f"ic tables missing tenant_isolation policy: {missing}"
    for r in rows:
        assert "tenant_id" in r.qual and "current_setting" in r.qual, (
            f"{r.tablename} policy is not the standard tenant predicate: {r.qual!r}"
        )


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
async def seeded_two_tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company + one ic_txn row, via the owner engine."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            txn_id = uuid.uuid4()
            session.add(Tenant(id=tid, name=f"IC154-{label}-{suffix}",
                               slug=f"ic154-{label}-{suffix}"))
            await session.flush()
            session.add(Company(id=cid, tenant_id=tid,
                                name=f"IC154-{label}-{suffix}", base_currency="AUD"))
            await session.flush()
            session.add(IcTxn(id=txn_id, tenant_id=tid, company_id=cid,
                              description=f"probe-{label}", status=IcTxnStatus.ACTIVE))
            await session.flush()
            out[label] = {"tenant_id": tid, "company_id": cid, "txn_id": txn_id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(text("DELETE FROM ic_txn WHERE id = :i"),
                                  {"i": row["txn_id"]})
            await session.execute(text("DELETE FROM companies WHERE id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM tenants WHERE id = :i"),
                                  {"i": row["tenant_id"]})
        await session.commit()


async def test_ic_txn_visible_to_own_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded_two_tenants["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM ic_txn WHERE id = :i"), {"i": a["txn_id"]}
            )
        ).all()
    assert len(visible) == 1, "tenant A cannot see its own ic_txn — RLS too tight"


async def test_ic_txn_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_txn = seeded_two_tenants["tenant_b"]["txn_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM ic_txn WHERE id = :i"), {"i": b_txn}
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's ic_txn {b_txn} — tenant_isolation broken/not FORCEd"
    )


async def test_ic_txn_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (await session.execute(text("SELECT count(*) FROM ic_txn"))).scalar_one()
    assert rows == 0, f"expected 0 ic_txn with no tenant set, got {rows} — not deny-by-default"


# --------------------------------------------------------------------------- #
# Service-layer LOCAL two-leg pair (superuser session, like test_cross_company_fk)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def local_pair_setup() -> AsyncIterator[dict[str, Any]]:
    """Two companies in the DEFAULT tenant, each with a control + contra account
    and a reciprocal ic_edges pair.

    Models the §3 directors-loan edge as a same-tenant LOCAL pair:
      originator (personal): control = 1-1500 Loan to SAE (ASSET), contra = 1-1000 Bank
      counterparty (SAE):    control = 2-2200 Directors Loan (LIABILITY), contra = 1-1000 Bank
    """
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        orig = Company(name=f"ICPersonal-{tag}", base_currency="AUD",
                       tenant_id=_DEFAULT_TENANT)
        cpty = Company(name=f"ICSae-{tag}", base_currency="AUD",
                       tenant_id=_DEFAULT_TENANT)
        session.add_all([orig, cpty])
        await session.flush()

        orig_control = Account(company_id=orig.id, tenant_id=_DEFAULT_TENANT,
                               code=f"1-15{tag[:2]}", name="Loan to SAE",
                               account_type=AccountType.ASSET)
        orig_contra = Account(company_id=orig.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Bank",
                              account_type=AccountType.ASSET)
        cpty_control = Account(company_id=cpty.id, tenant_id=_DEFAULT_TENANT,
                               code=f"2-22{tag[:2]}", name="Directors Loan",
                               account_type=AccountType.LIABILITY)
        cpty_contra = Account(company_id=cpty.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Bank",
                              account_type=AccountType.ASSET)
        session.add_all([orig_control, orig_contra, cpty_control, cpty_contra])
        await session.flush()

        session.add(IcEdge(tenant_id=_DEFAULT_TENANT, company_id=orig.id,
                           partner_company_id=cpty.id,
                           control_account_id=orig_control.id,
                           direction=IcEdgeDirection.ORIGINATOR))
        session.add(IcEdge(tenant_id=_DEFAULT_TENANT, company_id=cpty.id,
                           partner_company_id=orig.id,
                           control_account_id=cpty_control.id,
                           direction=IcEdgeDirection.COUNTERPARTY))
        await session.commit()

        data = {
            "orig_id": orig.id, "cpty_id": cpty.id,
            "orig_control": orig_control.id, "orig_contra": orig_contra.id,
            "cpty_control": cpty_control.id, "cpty_contra": cpty_contra.id,
        }
    yield data

    # Teardown: nuke any JEs/ic rows then accounts + companies.
    async with AsyncSessionLocal() as session:
        for cid in (data["orig_id"], data["cpty_id"]):
            await session.execute(text(
                "DELETE FROM ic_legs WHERE company_id = :c"), {"c": cid})
        await session.execute(text(
            "DELETE FROM ic_txn WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM ic_edges WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM journal_lines WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM journal_entries WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM accounts WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM companies WHERE id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.commit()


async def test_post_local_pair_happy_path(local_pair_setup: dict[str, Any]) -> None:
    d = local_pair_setup
    async with AsyncSessionLocal() as session:
        ic_txn = await ic_svc.post_local_pair(
            session,
            tenant_id=_DEFAULT_TENANT,
            originator_company_id=d["orig_id"],
            counterparty_company_id=d["cpty_id"],
            amount=Decimal("5000.00"),
            entry_date=date(2026, 6, 6),
            description="Director funds SAE working capital",
            originator_contra_account_id=d["orig_contra"],
            counterparty_contra_account_id=d["cpty_contra"],
            posted_by="test",
        )
    assert ic_txn.status == IcTxnStatus.ACTIVE

    async with AsyncSessionLocal() as session:
        legs = (await session.execute(
            select(IcLeg).where(IcLeg.ic_txn_id == ic_txn.id)
        )).scalars().all()
        assert len(legs) == 2, "expected exactly two ic_legs"
        sides = {leg.side for leg in legs}
        assert sides == {IcLegSide.ORIGINATOR, IcLegSide.COUNTERPARTY}

        for leg in legs:
            je = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == leg.journal_entry_id)
            )).scalar_one()
            assert je.status == EntryStatus.POSTED
            assert je.origin == JournalOrigin.INTERCOMPANY
            assert je.source_type == "ic_txn"
            assert je.source_id == ic_txn.id
            lines = (await session.execute(
                select(JournalLine).where(JournalLine.entry_id == je.id)
            )).scalars().all()
            assert len(lines) == 2
            tot_dr = sum(line.debit for line in lines)
            tot_cr = sum(line.credit for line in lines)
            assert tot_dr == tot_cr == Decimal("5000.00"), "leg must balance"

        # Directors-loan sign convention: originator control account is DEBITED
        # (due-from), counterparty control account is CREDITED (due-to).
        orig_control_line = (await session.execute(
            select(JournalLine).where(JournalLine.account_id == d["orig_control"])
        )).scalar_one()
        assert orig_control_line.debit == Decimal("5000.00")
        assert orig_control_line.credit == Decimal("0")
        cpty_control_line = (await session.execute(
            select(JournalLine).where(JournalLine.account_id == d["cpty_control"])
        )).scalar_one()
        assert cpty_control_line.credit == Decimal("5000.00")
        assert cpty_control_line.debit == Decimal("0")


async def test_post_local_pair_atomic_rollback(local_pair_setup: dict[str, Any]) -> None:
    """A bad counterparty contra account fails leg B → the WHOLE pair rolls back.

    Use a contra account that belongs to the ORIGINATOR (not the counterparty):
    _assert_account_owned rejects it before any JE is built, but the test also
    proves no partial ic_txn / JE survives.
    """
    d = local_pair_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(ic_svc.IntercompanyError):
            await ic_svc.post_local_pair(
                session,
                tenant_id=_DEFAULT_TENANT,
                originator_company_id=d["orig_id"],
                counterparty_company_id=d["cpty_id"],
                amount=Decimal("1000.00"),
                entry_date=date(2026, 6, 6),
                description="should not persist",
                originator_contra_account_id=d["orig_contra"],
                # WRONG: an originator account passed as the counterparty contra.
                counterparty_contra_account_id=d["orig_contra"],
                posted_by="test",
            )
    # Nothing persisted: no ic_txn, no ic_legs, no JEs for either company.
    async with AsyncSessionLocal() as session:
        n_txn = (await session.execute(text(
            "SELECT count(*) FROM ic_txn WHERE company_id IN (:a, :b)"),
            {"a": d["orig_id"], "b": d["cpty_id"]})).scalar_one()
        n_legs = (await session.execute(text(
            "SELECT count(*) FROM ic_legs WHERE company_id IN (:a, :b)"),
            {"a": d["orig_id"], "b": d["cpty_id"]})).scalar_one()
        n_je = (await session.execute(text(
            "SELECT count(*) FROM journal_entries WHERE company_id IN (:a, :b)"),
            {"a": d["orig_id"], "b": d["cpty_id"]})).scalar_one()
    assert n_txn == 0 and n_legs == 0 and n_je == 0, (
        f"partial pair survived rollback: txn={n_txn} legs={n_legs} je={n_je}"
    )


async def test_post_local_pair_atomic_on_leg_b_post_failure(
    local_pair_setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a failure at the SECOND post_in_txn → leg A must also roll back.

    Proves the single-commit atomicity at the post step, not just at validation.
    """
    d = local_pair_setup
    real_post = ic_svc.journal_svc.post_in_txn
    calls = {"n": 0}

    async def flaky_post(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # second leg
            raise RuntimeError("injected leg-B failure")
        return await real_post(*args, **kwargs)

    monkeypatch.setattr(ic_svc.journal_svc, "post_in_txn", flaky_post)

    async with AsyncSessionLocal() as session:
        with pytest.raises(RuntimeError, match="injected leg-B failure"):
            await ic_svc.post_local_pair(
                session,
                tenant_id=_DEFAULT_TENANT,
                originator_company_id=d["orig_id"],
                counterparty_company_id=d["cpty_id"],
                amount=Decimal("2500.00"),
                entry_date=date(2026, 6, 6),
                description="leg-B failure",
                originator_contra_account_id=d["orig_contra"],
                counterparty_contra_account_id=d["cpty_contra"],
                posted_by="test",
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        n_txn = (await session.execute(text(
            "SELECT count(*) FROM ic_txn WHERE company_id IN (:a, :b)"),
            {"a": d["orig_id"], "b": d["cpty_id"]})).scalar_one()
        n_je = (await session.execute(text(
            "SELECT count(*) FROM journal_entries WHERE company_id IN (:a, :b)"),
            {"a": d["orig_id"], "b": d["cpty_id"]})).scalar_one()
    assert n_txn == 0 and n_je == 0, (
        f"leg A survived a leg-B failure: txn={n_txn} je={n_je} — not atomic"
    )


async def test_reverse_local_pair(local_pair_setup: dict[str, Any]) -> None:
    d = local_pair_setup
    async with AsyncSessionLocal() as session:
        ic_txn = await ic_svc.post_local_pair(
            session,
            tenant_id=_DEFAULT_TENANT,
            originator_company_id=d["orig_id"],
            counterparty_company_id=d["cpty_id"],
            amount=Decimal("750.00"),
            entry_date=date(2026, 6, 6),
            description="to be reversed",
            originator_contra_account_id=d["orig_contra"],
            counterparty_contra_account_id=d["cpty_contra"],
            posted_by="test",
        )

    async with AsyncSessionLocal() as session:
        reversed_txn = await ic_svc.reverse_local_pair(
            session, ic_txn.id, tenant_id=_DEFAULT_TENANT,
            reversal_date=date(2026, 6, 7), posted_by="test",
        )
    assert reversed_txn.status == IcTxnStatus.REVERSED

    async with AsyncSessionLocal() as session:
        # Both original legs' JEs are REVERSED; two reversal JEs exist + linked.
        legs = (await session.execute(
            select(IcLeg).where(IcLeg.ic_txn_id == ic_txn.id)
        )).scalars().all()
        assert len(legs) == 4, "expected 2 original + 2 reversal ic_legs"
        je_statuses = []
        for leg in legs:
            je = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == leg.journal_entry_id)
            )).scalar_one()
            je_statuses.append(je.status)
        # Two originals flipped to REVERSED, two reversal entries POSTED.
        assert je_statuses.count(EntryStatus.REVERSED) == 2
        assert je_statuses.count(EntryStatus.POSTED) == 2

    # Idempotency: reversing an already-reversed txn raises.
    async with AsyncSessionLocal() as session:
        with pytest.raises(ic_svc.IntercompanyError, match="already reversed"):
            await ic_svc.reverse_local_pair(
                session, ic_txn.id, tenant_id=_DEFAULT_TENANT,
            )
