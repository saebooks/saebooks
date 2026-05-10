"""Tests for the NZ tax engine (M1 deliverable).

Covers:

* ``compute`` — standard / zero-rated / exempt determination.
* Direction mapping (output / input / none) including the exempt-
  purchase carve-out.
* ``boxes`` — pre-built ``GST101Report`` mapping.
* ``gst101_report`` end-to-end against a synthetic NZ company with a
  hand-rolled mini chart of accounts.
* Determinism + JSONable round-trip of ``TaxTreatment``.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import AccountType
from saebooks.services.tax_engine import (
    PostingContext,
    TaxTreatment,
    get_engine,
)
from saebooks.services.tax_engine.nz import (
    CODE_EXEMPT,
    CODE_STANDARD,
    CODE_ZERO_RATED,
    REPORTING_EXEMPT,
    REPORTING_STANDARD,
    REPORTING_ZERO_RATED,
    STANDARD_RATE,
    GST101Line,
    GST101Report,
    NZTaxEngine,
    gst101_report,
)

# ---------------------------------------------------------------------------
# compute() — sync, pure
# ---------------------------------------------------------------------------


def _ctx(
    *,
    account_type: AccountType,
    amount: Decimal,
    rate: Decimal | None = None,
    gst_amount: Decimal | None = None,
    tax_code: str | None = "GST",
    reporting_type: str | None = "standard",
) -> PostingContext:
    return PostingContext(
        company_id=uuid.uuid4(),
        jurisdiction="NZ",
        posting_date=date(2026, 4, 1),
        account_id=uuid.uuid4(),
        account_type=account_type,
        amount=amount,
        gst_amount=gst_amount,
        tax_code=tax_code,
        rate=rate,
        reporting_type=reporting_type,
    )


def test_get_engine_nz_returns_nztaxengine() -> None:
    engine = get_engine("NZ")
    assert isinstance(engine, NZTaxEngine)
    assert engine.jurisdiction == "NZ"


def test_compute_income_standard_supplied_gst_amount() -> None:
    """Standard sales line with the caller's pre-computed GST."""
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("200.00"),
        rate=STANDARD_RATE,
        gst_amount=Decimal("30.00"),
    )
    t = engine.compute(ctx)
    assert isinstance(t, TaxTreatment)
    assert t.jurisdiction == "NZ"
    assert t.code == "GST"
    assert t.rate == Decimal("0.15")
    assert t.base == Decimal("200.00")
    assert t.tax == Decimal("30.00")
    assert t.reporting_type == "standard"
    assert t.direction == "output"


def test_compute_expense_standard_derives_tax_from_rate() -> None:
    """Standard purchase line — engine derives 15% from the rate."""
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("400.00"),
        rate=STANDARD_RATE,
        gst_amount=None,
    )
    t = engine.compute(ctx)
    assert t.direction == "input"
    assert t.base == Decimal("400.00")
    assert t.tax == Decimal("60.00")


def test_compute_zero_rated_export_yields_zero_tax_with_output_direction() -> None:
    """Zero-rated exports — output direction (still reportable) but tax=0."""
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("500.00"),
        rate=Decimal("0"),
        gst_amount=None,
        tax_code=CODE_ZERO_RATED,
        reporting_type=REPORTING_ZERO_RATED,
    )
    t = engine.compute(ctx)
    assert t.direction == "output"
    assert t.tax == Decimal("0")
    assert t.code == "ZERO"
    assert t.reporting_type == "zero_rated"


def test_compute_exempt_income_has_output_direction_no_tax() -> None:
    """Exempt sales (e.g. financial services) — reportable as a supply,
    no GST."""
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("750.00"),
        rate=Decimal("0"),
        gst_amount=None,
        tax_code=CODE_EXEMPT,
        reporting_type=REPORTING_EXEMPT,
    )
    t = engine.compute(ctx)
    # On the income side, exempt is still output (the supply happened) —
    # but the period summary excludes it from Box 5 / Box 6.
    assert t.direction == "output"
    assert t.tax == Decimal("0")
    assert t.code == "EXEMPT"


def test_compute_exempt_expense_clamps_direction_to_none() -> None:
    """Exempt purchase — no input GST claim, so direction=='none'.

    This is the NZ-specific carve-out: an exempt expense classifies
    as 'none' (not 'input') so downstream summary code can't
    accidentally aggregate it onto Box 11 / Box 13."""
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("90.00"),
        rate=Decimal("0"),
        gst_amount=None,
        tax_code=CODE_EXEMPT,
        reporting_type=REPORTING_EXEMPT,
    )
    t = engine.compute(ctx)
    assert t.direction == "none"
    assert t.tax == Decimal("0")


def test_compute_equity_line_has_no_direction() -> None:
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.EQUITY,
        amount=Decimal("1000.00"),
        rate=None,
        gst_amount=None,
        tax_code=None,
        reporting_type=None,
    )
    t = engine.compute(ctx)
    assert t.direction == "none"
    assert t.tax == Decimal("0")
    # Defaults applied: code falls back to canonical "GST", reporting
    # falls back to "no_tax" so the snapshot isn't carrying a None.
    assert t.code == CODE_STANDARD
    assert t.reporting_type == "no_tax"


def test_compute_is_deterministic() -> None:
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("123.45"),
        rate=STANDARD_RATE,
        gst_amount=Decimal("18.52"),
    )
    a = engine.compute(ctx)
    b = engine.compute(ctx)
    assert a == b


def test_treatment_to_jsonable_includes_nz_jurisdiction() -> None:
    engine = get_engine("NZ")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("100.00"),
        rate=STANDARD_RATE,
        gst_amount=Decimal("15.00"),
    )
    payload = engine.compute(ctx).to_jsonable()
    assert payload["jurisdiction"] == "NZ"
    assert payload["code"] == "GST"
    assert payload["rate"] == "0.15"
    assert payload["base"] == "100.00"
    assert payload["tax"] == "15.00"
    assert payload["direction"] == "output"
    assert payload["reporting_type"] == "standard"


def test_validate_returns_empty_list() -> None:
    engine = get_engine("NZ")
    assert engine.validate(object()) == []


# ---------------------------------------------------------------------------
# boxes() — pre-built report mapping
# ---------------------------------------------------------------------------


def _sample_report() -> GST101Report:
    return GST101Report(
        period_from=date(2026, 4, 1),
        period_to=date(2026, 6, 30),
        box5=GST101Line("Box 5", "Total sales and income (incl. GST)", Decimal("11500.00")),
        box6=GST101Line("Box 6", "Zero-rated supplies", Decimal("1500.00")),
        box8=GST101Line("Box 8", "Total GST collected on sales", Decimal("1500.00")),
        box11=GST101Line("Box 11", "Total purchases (incl. GST)", Decimal("4600.00")),
        box13=GST101Line("Box 13", "Total GST claimed on purchases", Decimal("600.00")),
    )


def test_boxes_returns_ird_box_mapping() -> None:
    engine = get_engine("NZ")
    out = engine.boxes(_sample_report())
    assert out == {
        "Box 5": Decimal("11500.00"),
        "Box 6": Decimal("1500.00"),
        "Box 8": Decimal("1500.00"),
        "Box 11": Decimal("4600.00"),
        "Box 13": Decimal("600.00"),
    }


def test_boxes_accepts_duck_typed_report_attr() -> None:
    """Caller can wrap a report in an outer object — engine duck-types it."""
    engine = get_engine("NZ")

    class Wrapped:
        def __init__(self, r: GST101Report) -> None:
            self.report = r

    out = engine.boxes(Wrapped(_sample_report()))
    assert out["Box 8"] == Decimal("1500.00")


def test_boxes_rejects_unknown_period_type() -> None:
    engine = get_engine("NZ")
    with pytest.raises(NotImplementedError, match="GST101Report"):
        engine.boxes(object())


def test_gst101_report_payable_is_collected_minus_claimed() -> None:
    r = _sample_report()
    assert r.gst_payable == Decimal("900.00")


# ---------------------------------------------------------------------------
# gst101_report() — DB-driven period summary.
#
# Mirrors the AU bas_report tests in shape: a synthetic NZ company,
# hand-rolled accounts + tax_codes, a posted journal entry per
# scenario, then assertions on box totals.
# ---------------------------------------------------------------------------


_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_nz_company_with_minimal_coa() -> dict[str, uuid.UUID]:
    """Create a synthetic NZ company plus the four accounts and three
    tax_codes ``gst101_report`` needs to bucket entries.

    Returns the ids the test then uses to post journal entries.
    """
    from saebooks.models.account import Account
    from saebooks.models.company import Company
    from saebooks.models.tax_code import TaxCode

    async with AsyncSessionLocal() as session:
        co = Company(
            tenant_id=_TENANT_ID,
            name=f"NZ-engine-test-{uuid.uuid4().hex[:8]}",
            legal_name="NZ Engine Test Ltd",
            jurisdiction="NZ",
            base_currency="NZD",
        )
        session.add(co)
        await session.flush()

        sales = Account(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="4-1000",
            name="Sales",
            account_type=AccountType.INCOME,
        )
        expenses = Account(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="6-1000",
            name="Expenses",
            account_type=AccountType.EXPENSE,
        )
        gst_collected = Account(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="2-1310",
            name="GST Collected",
            account_type=AccountType.LIABILITY,
        )
        gst_paid = Account(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="2-1320",
            name="GST Paid",
            account_type=AccountType.LIABILITY,
        )
        for a in (sales, expenses, gst_collected, gst_paid):
            session.add(a)

        tc_std = TaxCode(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="GST",
            name="GST 15%",
            rate=Decimal("15.000"),
            tax_system="GST",
            reporting_type=REPORTING_STANDARD,
        )
        tc_zero = TaxCode(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="ZERO",
            name="Zero-rated supplies",
            rate=Decimal("0.000"),
            tax_system="GST",
            reporting_type=REPORTING_ZERO_RATED,
        )
        tc_exempt = TaxCode(
            company_id=co.id,
            tenant_id=_TENANT_ID,
            code="EXEMPT",
            name="Exempt supplies",
            rate=Decimal("0.000"),
            tax_system="GST",
            reporting_type=REPORTING_EXEMPT,
        )
        for t in (tc_std, tc_zero, tc_exempt):
            session.add(t)

        await session.commit()
        return {
            "company_id": co.id,
            "sales": sales.id,
            "expenses": expenses.id,
            "tc_std": tc_std.id,
            "tc_zero": tc_zero.id,
            "tc_exempt": tc_exempt.id,
        }


async def _post_entry(
    company_id: uuid.UUID,
    *,
    entry_date: date,
    lines: list[dict[str, object]],
) -> None:
    """Post a journal entry directly via the model — bypassing the
    services layer because we want to control the tax_code_id +
    gst_amount fields explicitly per line.
    """
    from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

    async with AsyncSessionLocal() as session:
        entry = JournalEntry(
            company_id=company_id,
            tenant_id=_TENANT_ID,
            ref=f"NZ-TEST-{uuid.uuid4().hex[:10]}",
            entry_date=entry_date,
            description="NZ engine test entry",
            status=EntryStatus.POSTED,
        )
        session.add(entry)
        await session.flush()
        for i, ln in enumerate(lines, start=1):
            session.add(
                JournalLine(
                    entry_id=entry.id,
                    line_no=i,
                    account_id=ln["account_id"],
                    description=ln.get("description", ""),
                    debit=ln.get("debit", Decimal("0")),
                    credit=ln.get("credit", Decimal("0")),
                    gst_amount=ln.get("gst_amount"),
                    tax_code_id=ln.get("tax_code_id"),
                )
            )
        await session.commit()


async def _cleanup_company(company_id: uuid.UUID) -> None:
    """Drop every row created by the test — accounts cascade to lines/
    entries via the company FK, but we delete the entry tree
    explicitly because the test runs against the shared seed_coa
    company DB."""
    from sqlalchemy import delete, select

    from saebooks.models.account import Account
    from saebooks.models.company import Company
    from saebooks.models.journal import JournalEntry, JournalLine
    from saebooks.models.tax_code import TaxCode

    async with AsyncSessionLocal() as session:
        # Delete journal lines (no company_id FK — find via entry).
        entry_ids = (
            await session.execute(
                select(JournalEntry.id).where(
                    JournalEntry.company_id == company_id
                )
            )
        ).scalars().all()
        if entry_ids:
            await session.execute(
                delete(JournalLine).where(JournalLine.entry_id.in_(entry_ids))
            )
        await session.execute(
            delete(JournalEntry).where(JournalEntry.company_id == company_id)
        )
        await session.execute(
            delete(TaxCode).where(TaxCode.company_id == company_id)
        )
        await session.execute(
            delete(Account).where(Account.company_id == company_id)
        )
        await session.execute(
            delete(Company).where(Company.id == company_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_gst101_report_buckets_standard_zero_rated_and_exempt() -> None:
    ids = await _make_nz_company_with_minimal_coa()
    company_id = ids["company_id"]
    try:
        # Standard sale: $100 net + $15 GST → $115 inclusive.
        # Box 5 picks up the $115, Box 8 picks up the $15.
        await _post_entry(
            company_id,
            entry_date=date(2026, 5, 1),
            lines=[
                {
                    "account_id": ids["sales"],
                    "description": "Standard sale",
                    "credit": Decimal("100.00"),
                    "gst_amount": Decimal("15.00"),
                    "tax_code_id": ids["tc_std"],
                },
            ],
        )

        # Zero-rated export: $500 net, no GST. Box 5 + Box 6 += $500.
        await _post_entry(
            company_id,
            entry_date=date(2026, 5, 2),
            lines=[
                {
                    "account_id": ids["sales"],
                    "description": "Zero-rated export",
                    "credit": Decimal("500.00"),
                    "gst_amount": Decimal("0"),
                    "tax_code_id": ids["tc_zero"],
                },
            ],
        )

        # Exempt sale: $50 net, financial-services-style — NOT reported
        # on Box 5 or Box 6.
        await _post_entry(
            company_id,
            entry_date=date(2026, 5, 3),
            lines=[
                {
                    "account_id": ids["sales"],
                    "description": "Exempt sale",
                    "credit": Decimal("50.00"),
                    "gst_amount": Decimal("0"),
                    "tax_code_id": ids["tc_exempt"],
                },
            ],
        )

        # Standard purchase: $200 net + $30 GST. Box 11 += $230, Box 13 += $30.
        await _post_entry(
            company_id,
            entry_date=date(2026, 5, 4),
            lines=[
                {
                    "account_id": ids["expenses"],
                    "description": "Standard purchase",
                    "debit": Decimal("200.00"),
                    "gst_amount": Decimal("30.00"),
                    "tax_code_id": ids["tc_std"],
                },
            ],
        )

        # Exempt purchase: $80, NOT reportable on Box 11 / Box 13.
        await _post_entry(
            company_id,
            entry_date=date(2026, 5, 5),
            lines=[
                {
                    "account_id": ids["expenses"],
                    "description": "Exempt purchase",
                    "debit": Decimal("80.00"),
                    "gst_amount": Decimal("0"),
                    "tax_code_id": ids["tc_exempt"],
                },
            ],
        )

        async with AsyncSessionLocal() as session:
            report = await gst101_report(
                session,
                company_id,
                from_date=date(2026, 4, 1),
                to_date=date(2026, 6, 30),
            )

        # Box 5: $115 (standard) + $500 (zero-rated) = $615.
        assert report.box5.amount == Decimal("615.00")
        # Box 6: $500 (zero-rated only).
        assert report.box6.amount == Decimal("500.00")
        # Box 8: $15.
        assert report.box8.amount == Decimal("15.00")
        # Box 11: $230 (standard purchase incl. GST).
        # Note exempt purchase is excluded.
        assert report.box11.amount == Decimal("230.00")
        # Box 13: $30.
        assert report.box13.amount == Decimal("30.00")
        # Net payable = $15 - $30 = -$15 (refund).
        assert report.gst_payable == Decimal("-15.00")
    finally:
        await _cleanup_company(company_id)


@pytest.mark.asyncio
async def test_gst101_report_excludes_entries_outside_window() -> None:
    ids = await _make_nz_company_with_minimal_coa()
    company_id = ids["company_id"]
    try:
        # In-window standard sale.
        await _post_entry(
            company_id,
            entry_date=date(2026, 5, 1),
            lines=[
                {
                    "account_id": ids["sales"],
                    "credit": Decimal("100.00"),
                    "gst_amount": Decimal("15.00"),
                    "tax_code_id": ids["tc_std"],
                },
            ],
        )
        # Out-of-window standard sale (next quarter).
        await _post_entry(
            company_id,
            entry_date=date(2026, 8, 1),
            lines=[
                {
                    "account_id": ids["sales"],
                    "credit": Decimal("9999.00"),
                    "gst_amount": Decimal("1500.00"),
                    "tax_code_id": ids["tc_std"],
                },
            ],
        )

        async with AsyncSessionLocal() as session:
            report = await gst101_report(
                session,
                company_id,
                from_date=date(2026, 4, 1),
                to_date=date(2026, 6, 30),
            )

        assert report.box5.amount == Decimal("115.00")
        assert report.box8.amount == Decimal("15.00")
    finally:
        await _cleanup_company(company_id)
