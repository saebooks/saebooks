"""Dutiable transaction event record type — service + RLS coverage (M1.5 · T5).

Covers migration 0181 (``dutiable_transaction_events`` table) and
``services/dutiable_events.py``. A ``DutiableTransactionEvent`` is the
first-class postable EVENT for an assessed stamp/transfer/conveyance/
securities/insurance duty — before this table ``stamp_duty_rate`` was a
rate-lookup table only, with nothing recording that a jurisdiction
actually assessed duty on a real transaction.

Structural / RLS (Postgres only):
  * RLS ENABLE + FORCE + a ``tenant_isolation`` policy on
    ``dutiable_transaction_events``.
  * Cross-tenant probe: a NOBYPASSRLS ``saebooks_app`` session scoped to
    tenant A cannot read tenant B's row; with no tenant set, zero rows
    (deny by default). Tenant-coherence trigger rejects a foreign
    company_id.

Service (superuser AsyncSessionLocal — mirrors tests/test_transfers.py):
  * Happy path: one balanced JE (Dr debit_account / Cr credit_account),
    origin=DUTY, source_type='dutiable_transaction_event',
    source_id=event.id, event.journal_entry_id linked, no GST line.
  * Validation: header account rejected, same account rejected,
    non-positive computed_duty rejected, unknown duty_type rejected,
    cross-company account rejected — nothing persists.
  * Reversal: JE -> REVERSED, event -> REVERSED, re-reverse raises.

Reference-DB rate lookup (``lookup_stamp_duty_rate``) is exercised
separately, gated by ``REFERENCE_MIGRATION_DATABASE_URL``, with its own
inline fixture row (no seed data required — see module docstring in
``services/dutiable_events.py``).
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
from sqlalchemy.exc import DBAPIError
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
from saebooks.models.dutiable_transaction_event import (
    DutiableEventStatus,
    DutiableTransactionEvent,
    DutyType,
)
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.tenant import Tenant
from saebooks.services import dutiable_events as svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TABLE = "dutiable_transaction_events"
_APP_ROLE_PASSWORD = "saebooks_app_test_pw"


# --------------------------------------------------------------------------- #
# Structural RLS assertions (the non-negotiable new-table checklist)
# --------------------------------------------------------------------------- #
async def test_dutiable_events_has_force_rls() -> None:
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
        "dutiable_transaction_events absent from pg_class — migration 0181 missing"
    )
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        "RLS not ENABLE+FORCE on dutiable_transaction_events — migration 0181 incomplete"
    )


async def test_dutiable_events_has_tenant_isolation_policy() -> None:
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
    assert row is not None, "dutiable_transaction_events missing tenant_isolation policy"
    assert "tenant_id" in row.qual and "current_setting" in row.qual, (
        f"dutiable_transaction_events policy is not the standard tenant predicate: {row.qual!r}"
    )


# --------------------------------------------------------------------------- #
# Cross-tenant probe via the NOBYPASSRLS saebooks_app role
# --------------------------------------------------------------------------- #
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
async def seeded_two_tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company + two accounts + one posted event."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            debit_id = uuid.uuid4()
            credit_id = uuid.uuid4()
            event_id = uuid.uuid4()
            session.add(
                Tenant(id=tid, name=f"DUTY-{label}-{suffix}", slug=f"duty-{label}-{suffix}")
            )
            await session.flush()
            session.add(
                Company(id=cid, tenant_id=tid, name=f"DUTY-{label}-{suffix}",
                        base_currency="AUD")
            )
            await session.flush()
            session.add(
                Account(id=debit_id, company_id=cid, tenant_id=tid,
                        code=f"6-90{suffix[:2]}", name="Duty Expense",
                        account_type=AccountType.EXPENSE)
            )
            session.add(
                Account(id=credit_id, company_id=cid, tenant_id=tid,
                        code=f"2-30{suffix[:2]}", name="Duty Payable",
                        account_type=AccountType.LIABILITY)
            )
            await session.flush()
            session.add(
                DutiableTransactionEvent(
                    id=event_id, tenant_id=tid, company_id=cid,
                    event_date=date(2026, 7, 9),
                    duty_type=DutyType.PROPERTY_TRANSFER.value,
                    jurisdiction="AUS",
                    dutiable_value=Decimal("500000.00"),
                    computed_duty=Decimal("15925.00"),
                    debit_account_id=debit_id, credit_account_id=credit_id,
                    status=DutiableEventStatus.POSTED,
                )
            )
            await session.flush()
            out[label] = {
                "tenant_id": tid, "company_id": cid, "event_id": event_id,
                "debit_id": debit_id, "credit_id": credit_id,
            }
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE id = :i"), {"i": row["event_id"]}
            )
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :i"), {"i": row["company_id"]}
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :i"), {"i": row["company_id"]}
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :i"), {"i": row["tenant_id"]}
            )
        await session.commit()


async def test_event_visible_to_own_tenant(
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
                text(f"SELECT id FROM {_TABLE} WHERE id = :i"), {"i": a["event_id"]}
            )
        ).all()
    assert len(visible) == 1, "tenant A cannot see its own event — RLS too tight"


async def test_event_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_event = seeded_two_tenants["tenant_b"]["event_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text(f"SELECT id FROM {_TABLE} WHERE id = :i"), {"i": b_event}
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's event {b_event} — tenant_isolation broken"
    )


async def test_event_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (await session.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    assert rows == 0, f"expected 0 dutiable events with no tenant set, got {rows}"


async def test_coherence_trigger_rejects_foreign_company(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    a = seeded_two_tenants["tenant_a"]
    b = seeded_two_tenants["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        # Use tenant_b's REAL debit/credit accounts (not random ids) so the
        # composite (account_id, company_id) FK is satisfied and the
        # tenant-coherence trigger is the sole possible failure — an FK
        # violation on a nonexistent account would otherwise race with the
        # trigger and make this assertion nondeterministic.
        with pytest.raises(DBAPIError, match="row-level security|tenant_coherence"):
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, company_id, event_date, duty_type, jurisdiction, "
                    " dutiable_value, computed_duty, debit_account_id, credit_account_id) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), '2026-07-09', "
                    "        'property_transfer', 'AUS', 1000, 100, "
                    "        CAST(:debit AS uuid), CAST(:credit AS uuid))"
                ).bindparams(
                    tid=str(a["tenant_id"]),
                    cid=str(b["company_id"]),
                    debit=str(b["debit_id"]),
                    credit=str(b["credit_id"]),
                )
            )


# --------------------------------------------------------------------------- #
# Service-layer (superuser session, like tests/test_transfers.py)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def event_setup() -> AsyncIterator[dict[str, Any]]:
    """One company in DEFAULT tenant with duty expense (EXPENSE), duty payable
    (LIABILITY), a header account, and a SISTER company's account."""
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        co = Company(name=f"DutyCo-{tag}", base_currency="AUD", tenant_id=_DEFAULT_TENANT)
        sister = Company(name=f"DutySis-{tag}", base_currency="AUD",
                         tenant_id=_DEFAULT_TENANT)
        session.add_all([co, sister])
        await session.flush()

        expense = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                          code=f"6-90{tag[:2]}", name="Duty Expense",
                          account_type=AccountType.EXPENSE)
        payable = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                          code=f"2-30{tag[:2]}", name="Duty Payable",
                          account_type=AccountType.LIABILITY)
        header = Account(company_id=co.id, tenant_id=_DEFAULT_TENANT,
                         code=f"6-00{tag[:2]}", name="Expenses (header)",
                         account_type=AccountType.EXPENSE, is_header=True)
        sister_bank = Account(company_id=sister.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Sister Bank",
                              account_type=AccountType.ASSET)
        session.add_all([expense, payable, header, sister_bank])
        await session.commit()
        data = {
            "company_id": co.id, "sister_id": sister.id,
            "expense": expense.id, "payable": payable.id, "header": header.id,
            "sister_bank": sister_bank.id,
        }
    yield data

    async with AsyncSessionLocal() as session:
        for cid in (data["company_id"], data["sister_id"]):
            await session.execute(text(
                f"DELETE FROM {_TABLE} WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM journal_lines WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM journal_entries WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM accounts WHERE company_id = :c"), {"c": cid})
            await session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": cid})
        await session.commit()


async def test_create_and_post_event_happy_path(event_setup: dict[str, Any]) -> None:
    d = event_setup
    async with AsyncSessionLocal() as session:
        event = await svc.create_and_post_event(
            session,
            tenant_id=_DEFAULT_TENANT,
            company_id=d["company_id"],
            event_date=date(2026, 7, 9),
            duty_type=DutyType.PROPERTY_TRANSFER.value,
            jurisdiction="AUS",
            sub_jurisdiction="AUQ",
            dutiable_value=Decimal("500000.00"),
            computed_duty=Decimal("15925.00"),
            debit_account_id=d["expense"],
            credit_account_id=d["payable"],
            description="Stamp duty on office purchase",
            reference="DUTY-1",
            posted_by="test",
        )
    assert event.status == DutiableEventStatus.POSTED
    assert event.journal_entry_id is not None
    assert event.sub_jurisdiction == "AUQ"

    async with AsyncSessionLocal() as session:
        je = (await session.execute(
            select(JournalEntry).where(JournalEntry.id == event.journal_entry_id)
        )).scalar_one()
        assert je.status == EntryStatus.POSTED
        assert je.origin == JournalOrigin.DUTY
        assert je.source_type == "dutiable_transaction_event"
        assert je.source_id == event.id

        lines = (await session.execute(
            select(JournalLine).where(JournalLine.entry_id == je.id)
        )).scalars().all()
        # Exactly two lines — no GST line.
        assert len(lines) == 2, "dutiable event must be exactly two lines, no GST"
        tot_dr = sum(line.debit for line in lines)
        tot_cr = sum(line.credit for line in lines)
        assert tot_dr == tot_cr == Decimal("15925.00")
        debit_line = next(line for line in lines if line.account_id == d["expense"])
        credit_line = next(line for line in lines if line.account_id == d["payable"])
        assert debit_line.debit == Decimal("15925.00") and debit_line.credit == Decimal("0")
        assert credit_line.credit == Decimal("15925.00") and credit_line.debit == Decimal("0")
        assert all(line.gst_amount in (None, Decimal("0")) for line in lines)


async def test_header_account_rejected(event_setup: dict[str, Any]) -> None:
    d = event_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DutiableEventError, match="header"):
            await svc.create_and_post_event(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                event_date=date(2026, 7, 9),
                duty_type=DutyType.PROPERTY_TRANSFER.value,
                jurisdiction="AUS",
                dutiable_value=Decimal("1000.00"),
                computed_duty=Decimal("10.00"),
                debit_account_id=d["header"],  # header — must be rejected
                credit_account_id=d["payable"],
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_same_account_rejected(event_setup: dict[str, Any]) -> None:
    d = event_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DutiableEventError, match="different"):
            await svc.create_and_post_event(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                event_date=date(2026, 7, 9),
                duty_type=DutyType.PROPERTY_TRANSFER.value,
                jurisdiction="AUS",
                dutiable_value=Decimal("1000.00"),
                computed_duty=Decimal("10.00"),
                debit_account_id=d["expense"],
                credit_account_id=d["expense"],
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_non_positive_duty_rejected(event_setup: dict[str, Any]) -> None:
    d = event_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DutiableEventError, match="positive"):
            await svc.create_and_post_event(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                event_date=date(2026, 7, 9),
                duty_type=DutyType.PROPERTY_TRANSFER.value,
                jurisdiction="AUS",
                dutiable_value=Decimal("1000.00"),
                computed_duty=Decimal("0.00"),
                debit_account_id=d["expense"],
                credit_account_id=d["payable"],
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_unknown_duty_type_rejected(event_setup: dict[str, Any]) -> None:
    d = event_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DutiableEventError, match="Unknown duty_type"):
            await svc.create_and_post_event(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                event_date=date(2026, 7, 9),
                duty_type="teleportation_tax",
                jurisdiction="AUS",
                dutiable_value=Decimal("1000.00"),
                computed_duty=Decimal("10.00"),
                debit_account_id=d["expense"],
                credit_account_id=d["payable"],
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_cross_company_account_rejected(event_setup: dict[str, Any]) -> None:
    """A sister company's account cannot be an event leg (app-layer guard +
    composite FK). Validated before any JE is built."""
    d = event_setup
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DutiableEventError):
            await svc.create_and_post_event(
                session,
                tenant_id=_DEFAULT_TENANT,
                company_id=d["company_id"],
                event_date=date(2026, 7, 9),
                duty_type=DutyType.PROPERTY_TRANSFER.value,
                jurisdiction="AUS",
                dutiable_value=Decimal("1000.00"),
                computed_duty=Decimal("10.00"),
                debit_account_id=d["expense"],
                credit_account_id=d["sister_bank"],  # belongs to sister company
            )
    await _assert_nothing_persisted(d["company_id"])


async def test_reverse_event(event_setup: dict[str, Any]) -> None:
    d = event_setup
    async with AsyncSessionLocal() as session:
        event = await svc.create_and_post_event(
            session,
            tenant_id=_DEFAULT_TENANT,
            company_id=d["company_id"],
            event_date=date(2026, 7, 9),
            duty_type=DutyType.PROPERTY_TRANSFER.value,
            jurisdiction="AUS",
            dutiable_value=Decimal("2000.00"),
            computed_duty=Decimal("75.00"),
            debit_account_id=d["expense"],
            credit_account_id=d["payable"],
            posted_by="test",
        )
        original_je_id = event.journal_entry_id

    async with AsyncSessionLocal() as session:
        reversed_event = await svc.reverse_event(
            session, event.id,
            tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            reversal_date=date(2026, 7, 10), posted_by="test",
        )
    assert reversed_event.status == DutiableEventStatus.REVERSED

    async with AsyncSessionLocal() as session:
        orig_je = (await session.execute(
            select(JournalEntry).where(JournalEntry.id == original_je_id)
        )).scalar_one()
        assert orig_je.status == EntryStatus.REVERSED
        all_je = (await session.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == d["company_id"])
        )).scalars().all()
        assert any(j.reversal_of_id == original_je_id for j in all_je), (
            "no reversal JE linked back to the original event JE"
        )

    # Idempotency: re-reversing raises.
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.DutiableEventError, match="already reversed"):
            await svc.reverse_event(
                session, event.id,
                tenant_id=_DEFAULT_TENANT, company_id=d["company_id"],
            )


async def _assert_nothing_persisted(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        n_events = (await session.execute(text(
            f"SELECT count(*) FROM {_TABLE} WHERE company_id = :c"),
            {"c": company_id})).scalar_one()
        n_je = (await session.execute(text(
            "SELECT count(*) FROM journal_entries WHERE company_id = :c"),
            {"c": company_id})).scalar_one()
    assert n_events == 0 and n_je == 0, (
        f"rejected event left state: events={n_events} je={n_je}"
    )


# --------------------------------------------------------------------------- #
# Reference-DB stamp-duty-rate lookup (optional, decoupled — inline fixture)
# --------------------------------------------------------------------------- #
pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_lookup_stamp_duty_rate_from_reference_db() -> None:
    """Inserts its own stamp_duty_rate bracket row (no seed data required —
    stamp_duty_rate owns no rows by default, see services/dutiable_events.py
    module docstring) and cleans it up, mirroring the AUQ insert/delete
    pattern in tests/seeds/test_jurisdiction_hierarchy.py."""
    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None
    await load_seeds("AU", version_tag="test-t5")

    async with ReferenceMigrationSession() as s:
        await s.execute(
            text(
                "INSERT INTO stamp_duty_rates "
                "(id, jurisdiction, state, transaction_type, lower_bound, "
                " upper_bound, rate, base_amount) "
                "VALUES (gen_random_uuid(), 'AUS', 'QLD', 'property_transfer', "
                "        0, 1000000, 3.5, 500)"
            )
        )
        await s.commit()

    try:
        # The runtime ReferenceSession (REFERENCE_DATABASE_URL) is not wired in
        # the standard test stack — only the migration session is. The lookup
        # is read-only, so exercise it through ReferenceMigrationSession, which
        # is what the reference-DB tests run against.
        async with ReferenceMigrationSession() as rs:
            duty = await svc.lookup_stamp_duty_rate(
                rs,
                jurisdiction="AUS",
                state="QLD",
                transaction_type="property_transfer",
                dutiable_value=Decimal("100000"),
            )
        assert duty == Decimal("500") + Decimal("100000") * Decimal("3.5") / Decimal("100")

        async with ReferenceMigrationSession() as rs:
            no_match = await svc.lookup_stamp_duty_rate(
                rs,
                jurisdiction="AUS",
                state="NSW",  # no NSW bracket seeded
                transaction_type="property_transfer",
                dutiable_value=Decimal("100000"),
            )
        assert no_match is None
    finally:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text(
                    "DELETE FROM stamp_duty_rates WHERE jurisdiction = 'AUS' "
                    "AND state = 'QLD' AND transaction_type = 'property_transfer'"
                )
            )
            await s.commit()
