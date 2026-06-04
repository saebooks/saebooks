"""C1 — cashbook journal lines must carry tax_code_id so BAS sees them.

Bug: ``record_cashbook_entry`` built JournalLine dicts with no
``tax_code_id``. The BAS aggregator (``services/tax_engine/au.py``
``bas_report``) outer-joins ``JournalLine.tax_code_id`` to
``TaxCode.reporting_type`` and keys every G-label off it, so a NULL
made cashbook-originated supplies invisible to BAS: a GST sale shows on
the P&L but G1/1A read 0.

These tests post cashbook entries via ``record_cashbook_entry`` and run
the real ``bas_report`` aggregation, asserting the supply lands in the
correct BAS box.

Isolation note: the test stack shares one seed company across modules,
so absolute BAS totals are contaminated by other modules' postings.
Every assertion here is therefore on the *delta* the entry produces
(BAS after minus BAS before), which is immune to pre-existing data.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.services import settings as settings_svc
from saebooks.services.cashbook import record_cashbook_entry
from saebooks.services.tax_engine.au import BASReport, bas_report

pytestmark = pytest.mark.postgres_only


# Wide window covering all entry dates used here; deltas make the exact
# bounds irrelevant, but a bounded window keeps the aggregation cheap.
_FROM = date(2026, 1, 1)
_TO = date(2026, 12, 31)
_ENTRY_DATE = date(2026, 5, 8)


@dataclass
class _BasDelta:
    g1: Decimal
    g2: Decimal
    g3: Decimal
    g10: Decimal
    g11: Decimal
    gst_collected: Decimal
    gst_paid: Decimal


async def _bas(company_id: uuid.UUID) -> BASReport:
    async with AsyncSessionLocal() as session:
        return await bas_report(session, company_id, from_date=_FROM, to_date=_TO)


def _delta(before: BASReport, after: BASReport) -> _BasDelta:
    return _BasDelta(
        g1=after.g1.amount - before.g1.amount,
        g2=after.g2.amount - before.g2.amount,
        g3=after.g3.amount - before.g3.amount,
        g10=after.g10.amount - before.g10.amount,
        g11=after.g11.amount - before.g11.amount,
        gst_collected=after.label_1a.amount - before.label_1a.amount,
        gst_paid=after.label_1b.amount - before.label_1b.amount,
    )


@pytest.fixture(autouse=True, scope="module")
async def _restore_seed_company_after_module():
    """Reset the shared seed company after this module (mirrors the
    teardown in test_cashbook.py — cashbook mode mutates shared state)."""
    yield
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        if co is None:
            return
        await session.execute(
            text(
                "UPDATE companies SET "
                "bookkeeping_mode = 'full', "
                "cashbook_default_bank_account_id = NULL, "
                "gst_registered = false "
                "WHERE id = :cid"
            ).bindparams(cid=co.id)
        )
        await session.commit()


async def _seed_company_into_cashbook_mode(
    *, gst_registered: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company not found"

        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id, Account.code == "1-1110"
                )
            )
        ).scalar_one_or_none()
        assert bank is not None, "AU CoA seed missing 1-1110 Bank"

        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.gst_registered = gst_registered
        if gst_registered:
            await settings_svc.set(session, "gst_collected_account_code", "2-1310")
            await settings_svc.set(session, "gst_paid_account_code", "2-1330")
            await settings_svc.set(session, "gst_auto_post", "true")
        await session.commit()
        return co.tenant_id, co.id


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


async def _post(
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    amount: Decimal,
    direction: str,
    category_code: str,
    prefix: str,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        je = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=_ENTRY_DATE,
            description=f"{category_code} test",
            amount=amount,
            direction=direction,
            category_code=category_code,
            idempotency_key=_key(prefix),
            actor="pytest",
        )
        return je.id


# ---------------------------------------------------------------------------
# KEYSTONE — a $1,100 incl-GST cashbook sale must move G1 by 1100, 1A by 100.
# Before the fix the cashbook line had no tax_code_id and the delta was 0.
# ---------------------------------------------------------------------------


async def test_keystone_cashbook_gst_sale_flows_to_bas_g1() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        gst_registered=True
    )
    before = await _bas(company_id)
    await _post(
        tenant_id, company_id,
        amount=Decimal("1100.00"), direction="income",
        category_code="INC_SALES", prefix="keystone-sale",
    )
    after = await _bas(company_id)
    d = _delta(before, after)

    assert d.g1 == Decimal("1100.00"), (
        f"BAS G1 should increase by 1100 (incl GST) for a cashbook GST "
        f"sale; moved by {d.g1}. NULL tax_code_id makes the line "
        f"invisible to BAS (delta 0 = the C1 bug)."
    )
    assert d.gst_collected == Decimal("100.00"), (
        f"1A (GST collected) should increase by 100; moved by {d.gst_collected}"
    )


# ---------------------------------------------------------------------------
# Per-category BAS routing — proves reporting_type drives the right box.
# ---------------------------------------------------------------------------


async def test_capital_purchase_flows_to_g10_not_g11() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        gst_registered=True
    )
    before = await _bas(company_id)
    await _post(
        tenant_id, company_id,
        amount=Decimal("2200.00"), direction="expense",
        category_code="CAP_PURCHASE", prefix="cap",
    )
    after = await _bas(company_id)
    d = _delta(before, after)

    assert d.g10 == Decimal("2200.00"), (
        f"Capital purchase should move G10 by 2200 incl GST; moved {d.g10}"
    )
    assert d.g11 == Decimal("0"), (
        f"Capital purchase must NOT touch G11; moved {d.g11}"
    )
    assert d.gst_paid == Decimal("200.00"), (
        f"1B (GST paid) should move by 200; moved {d.gst_paid}"
    )


async def test_taxable_expense_flows_to_g11() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        gst_registered=True
    )
    before = await _bas(company_id)
    await _post(
        tenant_id, company_id,
        amount=Decimal("1100.00"), direction="expense",
        category_code="EXP_MATERIALS", prefix="mat",
    )
    after = await _bas(company_id)
    d = _delta(before, after)

    assert d.g11 == Decimal("1100.00"), (
        f"Taxable expense should move G11 by 1100 incl GST; moved {d.g11}"
    )
    assert d.g10 == Decimal("0")
    assert d.gst_paid == Decimal("100.00")


async def test_gst_free_super_does_not_inflate_g11_or_1b() -> None:
    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        gst_registered=True
    )
    before = await _bas(company_id)
    await _post(
        tenant_id, company_id,
        amount=Decimal("1000.00"), direction="expense",
        category_code="EXP_SUPER", prefix="super",
    )
    after = await _bas(company_id)
    d = _delta(before, after)

    # gst_free expense: no GST paid, and not added to G10/G11 (the
    # GST-bearing purchase boxes).
    assert d.gst_paid == Decimal("0"), (
        f"GST-free super must not create a GST-paid claim; moved {d.gst_paid}"
    )
    assert d.g11 == Decimal("0")
    assert d.g10 == Decimal("0")


async def test_cashbook_line_carries_tax_code_id() -> None:
    """Direct unit assertion of the C1 fix: the category JE line now has
    a non-NULL tax_code_id resolving to the expected code."""
    from sqlalchemy import select as _select

    from saebooks.models.journal import JournalLine
    from saebooks.models.tax_code import TaxCode

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        gst_registered=True
    )
    je_id = await _post(
        tenant_id, company_id,
        amount=Decimal("1100.00"), direction="income",
        category_code="INC_SALES", prefix="stamp",
    )

    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                _select(JournalLine).where(JournalLine.entry_id == je_id)
            )
        ).scalars().all()
        stamped = [ln for ln in lines if ln.tax_code_id is not None]
        assert stamped, "no JE line carries a tax_code_id — C1 not fixed"
        tc_ids = {ln.tax_code_id for ln in stamped}
        codes = (
            await session.execute(
                _select(TaxCode.code).where(TaxCode.id.in_(tc_ids))
            )
        ).scalars().all()
        assert "GST" in set(codes), (
            f"income category line should resolve to GST; got {codes}"
        )


# ---------------------------------------------------------------------------
# Migration 0149 backfill — pre-fix NULL lines get stamped retroactively.
# ---------------------------------------------------------------------------


async def test_migration_backfill_stamps_legacy_null_lines() -> None:
    """Simulate the pre-fix world (a cashbook category line with NULL
    tax_code_id) and run migration 0149's step-3 backfill SQL. The line
    must end up stamped and the supply visible to BAS (delta restored)."""
    from sqlalchemy import select as _select
    from sqlalchemy import text as _text

    from saebooks.models.journal import JournalLine

    tenant_id, company_id = await _seed_company_into_cashbook_mode(
        gst_registered=True
    )
    base = await _bas(company_id)
    je_id = await _post(
        tenant_id, company_id,
        amount=Decimal("1100.00"), direction="income",
        category_code="INC_SALES", prefix="legacy",
    )

    # Reproduce the C1 bug: blank the tax_code on the category (credit) line.
    async with AsyncSessionLocal() as session:
        await session.execute(
            _text(
                "UPDATE journal_lines SET tax_code_id = NULL "
                "WHERE entry_id = :eid AND credit > 0"
            ).bindparams(eid=je_id)
        )
        await session.commit()

    broken = await _bas(company_id)
    assert (broken.g1.amount - base.g1.amount) == Decimal("0"), (
        "precondition: NULL line should be invisible to BAS (delta 0)"
    )

    # Run the migration's backfill SQL for INC_SALES (same statement as
    # migration 0149 step 3).
    async with AsyncSessionLocal() as session:
        await session.execute(
            _text(
                """
                WITH cb AS (
                    SELECT je.id AS entry_id, je.company_id,
                           je.attachments->'cashbook_meta'->>'direction' AS direction
                    FROM journal_entries je
                    WHERE je.attachments->'cashbook_meta' IS NOT NULL
                      AND je.attachments->'cashbook_meta'->>'category_code' = :cat
                ),
                tgt AS (
                    SELECT jl.id AS line_id, cb.company_id
                    FROM journal_lines jl
                    JOIN cb ON cb.entry_id = jl.entry_id
                    JOIN accounts a ON a.id = jl.account_id
                    WHERE jl.tax_code_id IS NULL
                      AND COALESCE(a.system_managed, false) = false
                      AND ((cb.direction = 'income' AND jl.credit > 0)
                        OR (cb.direction = 'expense' AND jl.debit > 0))
                ),
                resolved AS (
                    SELECT tgt.line_id,
                           COALESCE(
                             (SELECT t.id FROM tax_codes t
                              WHERE t.company_id = tgt.company_id AND t.code = :tc
                                AND t.archived_at IS NULL LIMIT 1),
                             (SELECT t.id FROM tax_codes t
                              WHERE t.company_id = tgt.company_id
                                AND t.reporting_type = :rep
                                AND t.archived_at IS NULL ORDER BY t.code LIMIT 1)
                           ) AS tc_id
                    FROM tgt
                )
                UPDATE journal_lines jl SET tax_code_id = resolved.tc_id
                FROM resolved
                WHERE jl.id = resolved.line_id AND resolved.tc_id IS NOT NULL
                """
            ),
            {"cat": "INC_SALES", "tc": "GST", "rep": "taxable"},
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                _select(JournalLine).where(JournalLine.entry_id == je_id)
            )
        ).scalars().all()
        assert any(ln.tax_code_id is not None for ln in lines), (
            "backfill did not stamp the legacy line"
        )

    fixed = await _bas(company_id)
    assert (fixed.g1.amount - base.g1.amount) == Decimal("1100.00"), (
        f"after backfill G1 delta should be 1100; got "
        f"{fixed.g1.amount - base.g1.amount}"
    )
    assert (fixed.label_1a.amount - base.label_1a.amount) == Decimal("100.00")
