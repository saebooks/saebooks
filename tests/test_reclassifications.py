"""Reclassification record type — service + RLS + API coverage.

Covers migration 0158 (``reclassifications`` table),
``services/reclassifications.py``, and ``/api/v1/reclassifications``. The
Reclassification is the first-class account-to-account classification move of
an already-posted amount (Gap 2): it posts ONE balanced, engine-generated
reclass JE that nets the OLD account to zero and lands the amount on the NEW
account, WITHOUT mutating the original posted entry (audit-preserved).

Structural / RLS (Postgres only):
  * RLS ENABLE + FORCE + a ``tenant_isolation`` policy on ``reclassifications``.
  * Cross-tenant probe: a NOBYPASSRLS ``saebooks_app`` session scoped to
    tenant A cannot read tenant B's ``reclassifications`` row; with no tenant
    set, zero rows (deny by default).

Service (superuser AsyncSessionLocal — mirrors tests/test_transfers.py):
  * Happy path (debit-natured): post $X 6-1000 -> child 6-1010 — one balanced
    reclass JE (Dr 6-1010 / Cr 6-1000), origin=RECLASSIFICATION,
    source_type='reclassification', source_id=reclass.id, row.journal_entry_id
    linked, no GST. Reports: A nets to 0, B +X for the pair; ORIGINAL entry
    untouched (still POSTED, lines unchanged).
  * Credit-natured mirror (income->income): JE is Dr from / Cr to.
  * source_entry_id is recorded and the source entry is never mutated.
  * Validation: cross-natural-side rejected (expense->income), header
    rejected, system-managed rejected, same account rejected, non-positive
    rejected, cross-company rejected — nothing persists.
  * Reversal: JE -> REVERSED, reclass -> REVERSED, re-reverse raises; the old
    account is restored (net back to original) after reversal.

API:
  * 401 without bearer; 201 create; 400 on cross-side; list + get; reverse + 409.
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
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.reclassification import (
    Reclassification,
    ReclassificationStatus,
)
from saebooks.models.tenant import Tenant
from saebooks.services import journal as journal_svc
from saebooks.services import reclassifications as svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TABLE = "reclassifications"

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# --------------------------------------------------------------------------- #
# Structural RLS assertions (the non-negotiable new-table checklist)
# --------------------------------------------------------------------------- #
async def test_reclassifications_has_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :n"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, (
        "reclassifications absent from pg_class — migration 0158 missing"
    )
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        "RLS not ENABLE+FORCE on reclassifications — migration 0158 incomplete"
    )


async def test_reclassifications_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT qual FROM pg_policies "
                    "WHERE tablename = :n AND policyname = 'tenant_isolation'"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, "reclassifications missing tenant_isolation policy"
    assert "tenant_id" in row.qual and "current_setting" in row.qual, (
        f"reclassifications policy is not the standard tenant predicate: {row.qual!r}"
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
    """Two tenants, each with a company + two expense accounts + one reclass row."""
    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            from_id = uuid.uuid4()
            to_id = uuid.uuid4()
            rc_id = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"RECLASS-{label}-{suffix}",
                    slug=f"reclass-{label}-{suffix}",
                )
            )
            await session.flush()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"RECLASS-{label}-{suffix}",
                    base_currency="AUD",
                )
            )
            await session.flush()
            session.add(
                Account(
                    id=from_id,
                    company_id=cid,
                    tenant_id=tid,
                    code=f"6-10{suffix[:2]}",
                    name="Materials",
                    account_type=AccountType.EXPENSE,
                )
            )
            session.add(
                Account(
                    id=to_id,
                    company_id=cid,
                    tenant_id=tid,
                    code=f"6-11{suffix[:2]}",
                    name="Materials (child)",
                    account_type=AccountType.EXPENSE,
                )
            )
            await session.flush()
            session.add(
                Reclassification(
                    id=rc_id,
                    tenant_id=tid,
                    company_id=cid,
                    from_account_id=from_id,
                    to_account_id=to_id,
                    amount=Decimal("100.00"),
                    reclass_date=date(2026, 6, 6),
                    status=ReclassificationStatus.POSTED,
                )
            )
            await session.flush()
            out[label] = {"tenant_id": tid, "company_id": cid, "rc_id": rc_id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text("DELETE FROM reclassifications WHERE id = :i"),
                {"i": row["rc_id"]},
            )
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :i"),
                {"i": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :i"), {"i": row["tenant_id"]}
            )
        await session.commit()


async def test_reclass_visible_to_own_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a = seeded_two_tenants["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM reclassifications WHERE id = :i"),
                {"i": a["rc_id"]},
            )
        ).all()
    assert len(visible) == 1, (
        "tenant A cannot see its own reclassification — RLS too tight"
    )


async def test_reclass_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_rc = seeded_two_tenants["tenant_b"]["rc_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM reclassifications WHERE id = :i"),
                {"i": b_rc},
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's reclassification {b_rc} — "
        "tenant_isolation broken"
    )


async def test_reclass_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with AppSession() as session, session.begin():
        rows = (
            await session.execute(text("SELECT count(*) FROM reclassifications"))
        ).scalar_one()
    assert rows == 0, f"expected 0 reclassifications with no tenant set, got {rows}"


# --------------------------------------------------------------------------- #
# Service-layer (superuser session, like tests/test_transfers.py)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def reclass_setup() -> AsyncIterator[dict[str, Any]]:
    """One company in DEFAULT tenant with: two expense accounts (parent +
    child, debit-natured), two income accounts (credit-natured), a bank
    (ASSET), a system-managed GST account, a header account, and a SISTER
    company's expense account (cross-company probe)."""
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        co = Company(name=f"RcCo-{tag}", base_currency="AUD", tenant_id=_DEFAULT_TENANT)
        sister = Company(
            name=f"RcSis-{tag}", base_currency="AUD", tenant_id=_DEFAULT_TENANT
        )
        session.add_all([co, sister])
        await session.flush()

        exp_parent = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"6-1000{tag[:1]}",
            name="Materials", account_type=AccountType.EXPENSE,
        )
        exp_child = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"6-1010{tag[:1]}",
            name="Materials - Steel", account_type=AccountType.EXPENSE,
        )
        inc_a = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"4-1000{tag[:1]}",
            name="Sales", account_type=AccountType.INCOME,
        )
        inc_b = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"4-1010{tag[:1]}",
            name="Sales - Export", account_type=AccountType.INCOME,
        )
        bank = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"1-10{tag[:2]}",
            name="Bank", account_type=AccountType.ASSET,
        )
        gst = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"2-30{tag[:2]}",
            name="GST Paid", account_type=AccountType.LIABILITY,
            system_managed=True,
        )
        header = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"6-00{tag[:2]}",
            name="Expenses (header)", account_type=AccountType.EXPENSE,
            is_header=True,
        )
        sister_exp = Account(
            company_id=sister.id, tenant_id=_DEFAULT_TENANT, code=f"6-10{tag[:2]}",
            name="Sister Materials", account_type=AccountType.EXPENSE,
        )
        session.add_all(
            [exp_parent, exp_child, inc_a, inc_b, bank, gst, header, sister_exp]
        )
        await session.commit()
        data = {
            "company_id": co.id, "sister_id": sister.id,
            "exp_parent": exp_parent.id, "exp_child": exp_child.id,
            "inc_a": inc_a.id, "inc_b": inc_b.id,
            "bank": bank.id, "gst": gst.id, "header": header.id,
            "sister_exp": sister_exp.id,
        }
    yield data

    async with AsyncSessionLocal() as session:
        for cid in (data["company_id"], data["sister_id"]):
            await session.execute(
                text("DELETE FROM reclassifications WHERE company_id = :c"),
                {"c": cid},
            )
            await session.execute(
                text("DELETE FROM journal_lines WHERE company_id = :c"), {"c": cid}
            )
            await session.execute(
                text("DELETE FROM journal_entries WHERE company_id = :c"), {"c": cid}
            )
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :c"), {"c": cid}
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :c"), {"c": cid}
            )
        await session.commit()


async def _post_original_expense(
    company_id: uuid.UUID, expense_account_id: uuid.UUID, bank_id: uuid.UUID,
    amount: Decimal,
) -> uuid.UUID:
    """Post a normal original expense JE (Dr expense / Cr bank) so there is a
    real posted balance to reclassify. Returns the JE id."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            entry_date=date(2026, 6, 1),
            tenant_id=_DEFAULT_TENANT,
            description="Original expense",
            lines=[
                {"account_id": expense_account_id, "debit": amount, "credit": 0},
                {"account_id": bank_id, "debit": 0, "credit": amount},
            ],
        )
        await journal_svc.post(
            session, entry.id, posted_by="test", tenant_id=_DEFAULT_TENANT,
            origin=JournalOrigin.EXPENSE, source_type="expense",
        )
        return entry.id


async def _account_net(company_id: uuid.UUID, account_id: uuid.UUID) -> Decimal:
    """balance = sum(debit) - sum(credit) over POSTED+REVERSED lines for the
    account (mirrors reports._account_balances)."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(jl.debit),0) - COALESCE(SUM(jl.credit),0) "
                    "FROM journal_lines jl "
                    "JOIN journal_entries je ON je.id = jl.entry_id "
                    "WHERE je.company_id = :c AND jl.account_id = :a "
                    "AND je.status IN ('POSTED','REVERSED')"
                ),
                {"c": company_id, "a": account_id},
            )
        ).scalar_one()
    return Decimal(str(row))


async def test_create_and_post_reclassification_happy_path(
    reclass_setup: dict[str, Any]
) -> None:
    """$500 from 6-1000 (parent) -> 6-1010 (child). Dr child / Cr parent;
    parent nets to 0, child +500; original entry untouched."""
    d = reclass_setup
    orig_je_id = await _post_original_expense(
        d["company_id"], d["exp_parent"], d["bank"], Decimal("500.00")
    )
    # Before reclass: parent +500, child 0.
    assert await _account_net(d["company_id"], d["exp_parent"]) == Decimal("500.00")
    assert await _account_net(d["company_id"], d["exp_child"]) == Decimal("0.00")

    async with AsyncSessionLocal() as session:
        reclass = await svc.create_and_post_reclassification(
            session,
            tenant_id=_DEFAULT_TENANT,
            company_id=d["company_id"],
            from_account_id=d["exp_parent"],
            to_account_id=d["exp_child"],
            amount=Decimal("500.00"),
            reclass_date=date(2026, 6, 6),
            reason="Reclassify steel into child account",
            source_entry_id=orig_je_id,
            created_by="test",
        )
    assert reclass.status == ReclassificationStatus.POSTED
    assert reclass.journal_entry_id is not None
    assert reclass.source_entry_id == orig_je_id

    async with AsyncSessionLocal() as session:
        je = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.id == reclass.journal_entry_id
                )
            )
        ).scalar_one()
        assert je.status == EntryStatus.POSTED
        assert je.origin == JournalOrigin.RECLASSIFICATION
        assert je.source_type == "reclassification"
        assert je.source_id == reclass.id

        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == je.id)
            )
        ).scalars().all()
        assert len(lines) == 2, "reclass must be exactly two lines, no GST"
        tot_dr = sum(line.debit for line in lines)
        tot_cr = sum(line.credit for line in lines)
        assert tot_dr == tot_cr == Decimal("500.00")
        # Dr child (to), Cr parent (from) — debit-natured pair.
        to_line = next(line for line in lines if line.account_id == d["exp_child"])
        from_line = next(line for line in lines if line.account_id == d["exp_parent"])
        assert to_line.debit == Decimal("500.00") and to_line.credit == Decimal("0")
        assert from_line.credit == Decimal("500.00") and from_line.debit == Decimal("0")
        assert all(line.gst_amount in (None, Decimal("0")) for line in lines)

        # Original entry untouched: still POSTED, still Dr parent 500.
        orig = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == orig_je_id)
            )
        ).scalar_one()
        assert orig.status == EntryStatus.POSTED
        orig_lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == orig_je_id)
            )
        ).scalars().all()
        orig_parent_line = next(
            line for line in orig_lines if line.account_id == d["exp_parent"]
        )
        assert orig_parent_line.debit == Decimal("500.00"), (
            "original posted entry was mutated — must be audit-preserved"
        )

    # Reports effect: parent nets to 0, child +500.
    assert await _account_net(d["company_id"], d["exp_parent"]) == Decimal("0.00")
    assert await _account_net(d["company_id"], d["exp_child"]) == Decimal("500.00")


async def test_credit_natured_mirror_direction(
    reclass_setup: dict[str, Any]
) -> None:
    """Income->income (credit-natured): JE is Dr from / Cr to, source income
    account still nets toward zero, target +amount."""
    d = reclass_setup
    # Seed an original income posting: Dr bank / Cr inc_a 300.
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session, company_id=d["company_id"], entry_date=date(2026, 6, 1),
            tenant_id=_DEFAULT_TENANT, description="Original income",
            lines=[
                {"account_id": d["bank"], "debit": Decimal("300.00"), "credit": 0},
                {"account_id": d["inc_a"], "debit": 0, "credit": Decimal("300.00")},
            ],
        )
        await journal_svc.post(
            session, entry.id, posted_by="test", tenant_id=_DEFAULT_TENANT,
            origin=JournalOrigin.MANUAL,
        )
    assert await _account_net(d["company_id"], d["inc_a"]) == Decimal("-300.00")

    async with AsyncSessionLocal() as session:
        reclass = await svc.create_and_post_reclassification(
            session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            from_account_id=d["inc_a"], to_account_id=d["inc_b"],
            amount=Decimal("300.00"), reclass_date=date(2026, 6, 6),
            reason="reclass income", created_by="test",
        )

    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(
                    JournalLine.entry_id == reclass.journal_entry_id
                )
            )
        ).scalars().all()
        from_line = next(line for line in lines if line.account_id == d["inc_a"])
        to_line = next(line for line in lines if line.account_id == d["inc_b"])
        # Mirror: Dr from / Cr to.
        assert from_line.debit == Decimal("300.00") and from_line.credit == Decimal("0")
        assert to_line.credit == Decimal("300.00") and to_line.debit == Decimal("0")

    # Source income nets to 0, target -300 (credit balance).
    assert await _account_net(d["company_id"], d["inc_a"]) == Decimal("0.00")
    assert await _account_net(d["company_id"], d["inc_b"]) == Decimal("-300.00")


async def test_cross_natural_side_rejected(reclass_setup: dict[str, Any]) -> None:
    """Expense -> income is NOT a classification move — rejected."""
    d = reclass_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError, match="natural"):
            await svc.create_and_post_reclassification(
                session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
                from_account_id=d["exp_parent"], to_account_id=d["inc_a"],
                amount=Decimal("10.00"), reclass_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_header_account_rejected(reclass_setup: dict[str, Any]) -> None:
    d = reclass_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError, match="header"):
            await svc.create_and_post_reclassification(
                session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
                from_account_id=d["header"], to_account_id=d["exp_child"],
                amount=Decimal("10.00"), reclass_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_system_managed_rejected(reclass_setup: dict[str, Any]) -> None:
    """A system-managed (GST) account can never be hand-reclassified."""
    d = reclass_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError, match="system-managed"):
            await svc.create_and_post_reclassification(
                session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
                from_account_id=d["gst"], to_account_id=d["exp_child"],
                amount=Decimal("10.00"), reclass_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_same_account_rejected(reclass_setup: dict[str, Any]) -> None:
    d = reclass_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError, match="different"):
            await svc.create_and_post_reclassification(
                session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
                from_account_id=d["exp_parent"], to_account_id=d["exp_parent"],
                amount=Decimal("10.00"), reclass_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_non_positive_amount_rejected(reclass_setup: dict[str, Any]) -> None:
    d = reclass_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError, match="positive"):
            await svc.create_and_post_reclassification(
                session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
                from_account_id=d["exp_parent"], to_account_id=d["exp_child"],
                amount=Decimal("0.00"), reclass_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_cross_company_account_rejected(
    reclass_setup: dict[str, Any]
) -> None:
    """A sister company's account cannot be a reclass leg (app-layer guard +
    composite FK). Validated before any JE is built."""
    d = reclass_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError):
            await svc.create_and_post_reclassification(
                session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
                from_account_id=d["exp_parent"], to_account_id=d["sister_exp"],
                amount=Decimal("10.00"), reclass_date=date(2026, 6, 6),
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_reverse_reclassification(reclass_setup: dict[str, Any]) -> None:
    """Reverse restores the old account; re-reverse raises."""
    d = reclass_setup
    await _post_original_expense(
        d["company_id"], d["exp_parent"], d["bank"], Decimal("750.00")
    )
    async with AsyncSessionLocal() as session:
        reclass = await svc.create_and_post_reclassification(
            session, tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            from_account_id=d["exp_parent"], to_account_id=d["exp_child"],
            amount=Decimal("750.00"), reclass_date=date(2026, 6, 6),
            created_by="test",
        )
        original_je_id = reclass.journal_entry_id
    # After reclass: parent 0, child 750.
    assert await _account_net(d["company_id"], d["exp_parent"]) == Decimal("0.00")
    assert await _account_net(d["company_id"], d["exp_child"]) == Decimal("750.00")

    async with AsyncSessionLocal() as session:
        reversed_rc = await svc.reverse_reclassification(
            session, reclass.id,
            tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            reversal_date=date(2026, 6, 7), posted_by="test",
        )
    assert reversed_rc.status == ReclassificationStatus.REVERSED

    async with AsyncSessionLocal() as session:
        orig_je = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == original_je_id)
            )
        ).scalar_one()
        assert orig_je.status == EntryStatus.REVERSED
        all_je = (
            await session.execute(
                select(JournalEntry).where(
                    JournalEntry.company_id == d["company_id"]
                )
            )
        ).scalars().all()
        assert any(j.reversal_of_id == original_je_id for j in all_je), (
            "no reversal JE linked back to the original reclass JE"
        )

    # After reversal: parent back to 750, child back to 0 (the reclass undone).
    assert await _account_net(d["company_id"], d["exp_parent"]) == Decimal("750.00")
    assert await _account_net(d["company_id"], d["exp_child"]) == Decimal("0.00")

    # Idempotency: re-reversing raises.
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReclassificationError, match="already reversed"):
            await svc.reverse_reclassification(
                session, reclass.id,
                tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            )


async def _assert_nothing_persisted(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        n_rc = (
            await session.execute(
                text("SELECT count(*) FROM reclassifications WHERE company_id = :c"),
                {"c": company_id},
            )
        ).scalar_one()
        # Only the rejected reclass should have left no JE; an original posted
        # expense from a prior step may exist, so scope to reclass JEs.
        n_je = (
            await session.execute(
                text(
                    "SELECT count(*) FROM journal_entries "
                    "WHERE company_id = :c AND origin = 'RECLASSIFICATION'"
                ),
                {"c": company_id},
            )
        ).scalar_one()
    assert n_rc == 0 and n_je == 0, (
        f"rejected reclassification left state: reclassifications={n_rc} "
        f"reclass_je={n_je}"
    )


# --------------------------------------------------------------------------- #
# API contract
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def api_company() -> AsyncIterator[dict[str, Any]]:
    """A company in DEFAULT tenant with a parent + child expense account and an
    income account (for the cross-side 400), for the HTTP surface."""
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        co = Company(name=f"RcApi-{tag}", base_currency="AUD", tenant_id=_DEFAULT_TENANT)
        session.add(co)
        await session.flush()
        exp_parent = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"6-1000{tag[:1]}",
            name="Materials", account_type=AccountType.EXPENSE,
        )
        exp_child = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"6-1010{tag[:1]}",
            name="Materials - Steel", account_type=AccountType.EXPENSE,
        )
        income = Account(
            company_id=co.id, tenant_id=_DEFAULT_TENANT, code=f"4-10{tag[:2]}",
            name="Sales", account_type=AccountType.INCOME,
        )
        session.add_all([exp_parent, exp_child, income])
        await session.commit()
        data = {
            "company_id": co.id, "exp_parent": exp_parent.id,
            "exp_child": exp_child.id, "income": income.id,
        }
    yield data
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM reclassifications WHERE company_id = :c"),
            {"c": data["company_id"]},
        )
        await session.execute(
            text("DELETE FROM journal_lines WHERE company_id = :c"),
            {"c": data["company_id"]},
        )
        await session.execute(
            text("DELETE FROM journal_entries WHERE company_id = :c"),
            {"c": data["company_id"]},
        )
        await session.execute(
            text("DELETE FROM accounts WHERE company_id = :c"),
            {"c": data["company_id"]},
        )
        await session.execute(
            text("DELETE FROM companies WHERE id = :c"), {"c": data["company_id"]}
        )
        await session.commit()


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def unauth_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_api_unauth_rejected(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/reclassifications")
    assert r.status_code == 401


async def test_api_create_list_get_reverse(
    api_client: AsyncClient, api_company: dict[str, Any]
) -> None:
    cid = str(api_company["company_id"])
    hdr = {"X-Company-Id": cid}
    body = {
        "from_account_id": str(api_company["exp_parent"]),
        "to_account_id": str(api_company["exp_child"]),
        "amount": "320.00",
        "reclass_date": "2026-06-06",
        "reason": "Reclass via API",
    }
    r = await api_client.post("/api/v1/reclassifications", json=body, headers=hdr)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["amount"] == "320.00"
    assert out["status"] == "POSTED"
    assert out["journal_entry_id"]
    rc_id = out["id"]

    # List
    r = await api_client.get("/api/v1/reclassifications", headers=hdr)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert any(it["id"] == rc_id for it in items)

    # Get
    r = await api_client.get(f"/api/v1/reclassifications/{rc_id}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["id"] == rc_id

    # Reverse
    r = await api_client.post(
        f"/api/v1/reclassifications/{rc_id}/reverse", json={}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVERSED"

    # Re-reverse -> 409
    r = await api_client.post(
        f"/api/v1/reclassifications/{rc_id}/reverse", json={}, headers=hdr
    )
    assert r.status_code == 409, r.text


async def test_api_create_cross_side_400(
    api_client: AsyncClient, api_company: dict[str, Any]
) -> None:
    hdr = {"X-Company-Id": str(api_company["company_id"])}
    body = {
        "from_account_id": str(api_company["exp_parent"]),
        "to_account_id": str(api_company["income"]),  # cross natural side
        "amount": "50.00",
        "reclass_date": "2026-06-06",
    }
    r = await api_client.post("/api/v1/reclassifications", json=body, headers=hdr)
    assert r.status_code == 400, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "reclassification_invalid"


async def test_api_get_unknown_404(
    api_client: AsyncClient, api_company: dict[str, Any]
) -> None:
    hdr = {"X-Company-Id": str(api_company["company_id"])}
    r = await api_client.get(
        f"/api/v1/reclassifications/{uuid.uuid4()}", headers=hdr
    )
    assert r.status_code == 404, r.text
