"""NZ tax engine — unit compute tests + the GST101A golden period.

Unit tests: ``NZTaxEngine.compute`` direction/derivation semantics (no
DB). Golden tests: a synthetic NZ company (``Company.jurisdiction ==
"NZ"`` — the posting dispatcher resolves the REAL NZTaxEngine), a
period of real posted journal entries, aggregated through the REAL
``_aggregate_ledger_by_box`` + ``_evaluate_formula_boxes`` passes
against the REAL NZ GST101 seed YAML — asserting EVERY box, including
the ones that must read 0 (feeder-collision guard).

Why not ``generate_return(jurisdiction="NZ", ...)``: same reason as the
EE KMD golden tests in ``test_tax_return_generator.py`` —
REFERENCE_DATABASE_URL is never configured in this harness, so
``generate_return`` would fall to ``_FALLBACK_BOX_DEFINITIONS`` (AU/BAS
only; NZ is reference-DB-only by design). Reading the real seed file +
driving the two real aggregation passes is the strongest achievable
test; the reference-row fetch itself is covered generically by the
gated Packet-1 test.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.jurisdictions.nz.tax import NZTaxEngine
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import settings as settings_svc
from saebooks.services.tax_engine.types import PostingContext
from saebooks.services.tax_return_generator import (
    _aggregate_ledger_by_box,
    _BoxDefRow,
    _evaluate_formula_boxes,
    _parse_box_definition,
)

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Unit — engine compute semantics (no DB).
# ---------------------------------------------------------------------------


def _pc(**kw) -> PostingContext:
    d = dict(
        company_id=uuid.uuid4(),
        jurisdiction="NZ",
        posting_date=date(2026, 5, 1),
        account_id=uuid.uuid4(),
        account_type=AccountType.INCOME,
        amount=Decimal("100.00"),
        tax_code="GST",
        rate=Decimal("15.000"),
        reporting_type="taxable",
    )
    d.update(kw)
    return PostingContext(**d)


def test_nz_compute_derives_fifteen_percent_from_percentage_points() -> None:
    t = NZTaxEngine().compute(_pc())
    assert t.jurisdiction == "NZ"
    assert t.tax == Decimal("15.00")   # 100 * 15 / 100 — EE convention, not AU's rate-as-fraction
    assert t.direction == "output"
    assert t.reporting_type == "taxable"


def test_nz_compute_trusts_caller_supplied_gst_amount() -> None:
    t = NZTaxEngine().compute(_pc(gst_amount=Decimal("13.05")))
    assert t.tax == Decimal("13.05")


def test_nz_compute_direction_from_account_type() -> None:
    e = NZTaxEngine()
    assert e.compute(_pc(account_type=AccountType.EXPENSE)).direction == "input"
    assert e.compute(_pc(account_type=AccountType.ASSET)).direction == "input"
    assert (
        e.compute(_pc(account_type=AccountType.LIABILITY, rate=Decimal("0"))).direction
        == "none"
    )


def test_nz_compute_components_is_single_component() -> None:
    e = NZTaxEngine()
    components = e.compute_components(_pc())
    assert len(components) == 1
    assert components[0] == e.compute(_pc())


def test_nz_accommodation_apportionment_is_callers_value_not_a_rate() -> None:
    # s 10(6): a $1,000 long-stay accommodation supply is taxed on 60%
    # of value — the CALLER supplies the apportioned base (600) at the
    # ordinary 15%; the engine has no 9% special case.
    t = NZTaxEngine().compute(
        _pc(amount=Decimal("600.00"), tax_code="ACCOM_LT")
    )
    assert t.rate == Decimal("15.000")
    assert t.tax == Decimal("90.00")   # == 9% of the $1,000 consideration


def test_nz_boxes_protocol_method_points_at_gst101_report() -> None:
    with pytest.raises(NotImplementedError, match="gst101_report"):
        NZTaxEngine().boxes(object())


# ---------------------------------------------------------------------------
# Golden — real postings through the real NZ dispatcher + real seed boxes.
# ---------------------------------------------------------------------------

_NZ_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "NZ"
    / "tax_return_box_definitions.yaml"
)


def _nz_gst101_parsed_boxes() -> list:
    doc = yaml.safe_load(_NZ_SEED_PATH.read_text())
    rows = [r for r in doc["rows"] if r["return_type"] == "GST101"]
    return [
        _parse_box_definition(
            _BoxDefRow(
                box_code=r["box_code"],
                box_label=r["box_label"],
                aggregation=r["aggregation"],
                feeder_tax_codes=r.get("feeder_tax_codes") or [],
                display_order=r["display_order"],
                formula=r.get("formula"),
            )
        )
        for r in rows
    ]


# The payable golden period, in full (every box asserted).
_NZ_GOLDEN_EXPECTED: dict[str, Decimal] = {
    "5": Decimal("16500.00"),        # 11,500 taxable-inclusive + 5,000 zero-rated
    "5_TAXABLE": Decimal("11500.00"),
    "5_ZERO": Decimal("5000.00"),
    "6": Decimal("5000.00"),
    "7": Decimal("11500.00"),
    "8": Decimal("1500.00"),         # 11,500 x 3/23
    "9": Decimal("0.00"),
    "10": Decimal("1500.00"),
    "11": Decimal("3450.00"),        # (2,000+300) taxable + (1,000+150) capital, GST-inclusive
    "12": Decimal("450.00"),         # 3,450 x 3/23
    "13": Decimal("0.00"),
    "14": Decimal("450.00"),
    "15": Decimal("1050.00"),        # payable
}


async def _make_nz_company() -> uuid.UUID:
    """Throwaway NZ company (own chart, own NZ tax codes) so absolute
    box totals are isolated — the same shape as the EE KMD golden
    company in test_tax_return_generator.py."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"GST101 Golden {company_id.hex[:8]}",
                base_currency="NZD",
                fin_year_start_month=4,
                audit_mode="immutable",
                jurisdiction="NZ",
            )
        )
        await session.flush()

        # GST auto-post settings are global (same convention/codes as
        # the other golden suites — idempotent alongside them).
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")

        accounts = {
            "bank": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1110", name="Bank", account_type=AccountType.ASSET),
            "income": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="4-1000", name="Sales", account_type=AccountType.INCOME),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Purchases", account_type=AccountType.EXPENSE),
            "fixed_asset": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1500", name="Fixed Assets", account_type=AccountType.ASSET),
            "gst_collected": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1310", name="GST Collected", account_type=AccountType.LIABILITY),
            "gst_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="GST Paid", account_type=AccountType.ASSET),
        }
        for acct in accounts.values():
            session.add(acct)
        await session.flush()

        tax_codes = {
            "taxable": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="NZ-GST", name="NZ GST 15%", rate=Decimal("15.000"), tax_system="GST", jurisdiction="NZ", reporting_type="taxable"),
            "zero_rated": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="NZ-ZERO", name="NZ zero-rated", rate=Decimal("0.000"), tax_system="GST", jurisdiction="NZ", reporting_type="zero_rated"),
            "exempt": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="NZ-EXEMPT", name="NZ exempt", rate=Decimal("0.000"), tax_system="GST", jurisdiction="NZ", reporting_type="exempt"),
            "capital": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="NZ-CAP", name="NZ capital 15%", rate=Decimal("15.000"), tax_system="GST", jurisdiction="NZ", reporting_type="capital"),
        }
        for tc in tax_codes.values():
            session.add(tc)
        await session.commit()

    return company_id


async def _company_fixtures(company_id: uuid.UUID) -> tuple[dict, dict]:
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }
    accounts = {
        "bank": by_code["1-1110"], "income": by_code["4-1000"],
        "expense": by_code["5-1000"], "fixed_asset": by_code["1-1500"],
    }
    return accounts, tax_by_type


async def _post_two_line(
    company_id: uuid.UUID,
    *,
    entry_date: date,
    description: str,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    debit_amount: Decimal,
    credit_amount: Decimal,
    tax_line: str,
    tax_code_id: uuid.UUID | None,
    gst: Decimal,
) -> None:
    async with AsyncSessionLocal() as session:
        debit_line: dict[str, object] = {"account_id": debit_account_id, "debit": debit_amount, "credit": Decimal("0")}
        credit_line: dict[str, object] = {"account_id": credit_account_id, "debit": Decimal("0"), "credit": credit_amount}
        if tax_line == "debit":
            debit_line["tax_code_id"] = tax_code_id
            debit_line["gst_amount"] = gst
        else:
            credit_line["tax_code_id"] = tax_code_id
            credit_line["gst_amount"] = gst
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=description,
            lines=[debit_line, credit_line],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-gst101-golden")


async def _post_sale(company_id, accounts, tax_code_id, *, entry_date, net, gst, label) -> None:
    await _post_two_line(
        company_id, entry_date=entry_date, description=f"GST101 golden — {label}",
        debit_account_id=accounts["bank"], credit_account_id=accounts["income"],
        debit_amount=net + gst, credit_amount=net,
        tax_line="credit", tax_code_id=tax_code_id, gst=gst,
    )


async def _post_purchase(company_id, accounts, tax_code_id, *, entry_date, expense_account_id, net, gst, label) -> None:
    await _post_two_line(
        company_id, entry_date=entry_date, description=f"GST101 golden — {label}",
        debit_account_id=expense_account_id, credit_account_id=accounts["bank"],
        debit_amount=net, credit_amount=net + gst,
        tax_line="debit", tax_code_id=tax_code_id, gst=gst,
    )


async def _gst101_box_vector(
    company_id: uuid.UUID,
    *,
    from_date: date,
    to_date: date,
    manual_values: dict[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    parsed = _nz_gst101_parsed_boxes()
    async with AsyncSessionLocal() as session:
        ledger_amounts = await _aggregate_ledger_by_box(
            session, parsed,
            company_id=company_id, tenant_id=None,
            from_date=from_date, to_date=to_date,
            statuses=(EntryStatus.POSTED,), exclude_archived=False,
        )
    return _evaluate_formula_boxes(
        parsed, ledger_amounts, return_type="GST101", manual_values=manual_values
    )


async def _post_payable_period(company_id, accounts, tax_by_type, entry_date: date) -> None:
    await _post_sale(company_id, accounts, tax_by_type["taxable"], entry_date=entry_date, net=Decimal("10000.00"), gst=Decimal("1500.00"), label="standard 15%")
    await _post_sale(company_id, accounts, tax_by_type["zero_rated"], entry_date=entry_date, net=Decimal("5000.00"), gst=Decimal("0.00"), label="zero-rated export")
    await _post_sale(company_id, accounts, tax_by_type["exempt"], entry_date=entry_date, net=Decimal("800.00"), gst=Decimal("0.00"), label="exempt residential rent")
    await _post_purchase(company_id, accounts, tax_by_type["taxable"], entry_date=entry_date, expense_account_id=accounts["expense"], net=Decimal("2000.00"), gst=Decimal("300.00"), label="standard purchase")
    await _post_purchase(company_id, accounts, tax_by_type["capital"], entry_date=entry_date, expense_account_id=accounts["fixed_asset"], net=Decimal("1000.00"), gst=Decimal("150.00"), label="capital purchase")


async def test_nz_gst101_golden_period_payable() -> None:
    """The payable golden period — every one of the 13 box codes
    asserted, so a box that should read 0 but doesn't (feeder
    collision; e.g. the exempt sale leaking into Box 5) fails loudly.
    Exercises the two-leg Box 5 formula, the 3/23 extraction (exact:
    11,500 x 3/23 = 1,500.00) and the signed Box 15."""
    company_id = await _make_nz_company()
    accounts, tax_by_type = await _company_fixtures(company_id)
    await _post_payable_period(company_id, accounts, tax_by_type, date(2026, 5, 15))

    amounts = await _gst101_box_vector(
        company_id, from_date=date(2026, 5, 1), to_date=date(2026, 6, 30)
    )
    for box_code, expected in _NZ_GOLDEN_EXPECTED.items():
        assert amounts.get(box_code) == expected, (
            f"GST101 box {box_code!r} expected {expected}, got {amounts.get(box_code)}"
        )


async def test_nz_gst101_golden_period_refund_signed_negative() -> None:
    """Refund variant: input credit exceeds output tax and Box 15 goes
    NEGATIVE (the GST101A is a signed single box — no max(0,·) split)."""
    company_id = await _make_nz_company()
    accounts, tax_by_type = await _company_fixtures(company_id)
    entry_date = date(2026, 7, 15)
    await _post_sale(company_id, accounts, tax_by_type["taxable"], entry_date=entry_date, net=Decimal("2000.00"), gst=Decimal("300.00"), label="small sale")
    await _post_purchase(company_id, accounts, tax_by_type["taxable"], entry_date=entry_date, expense_account_id=accounts["expense"], net=Decimal("10000.00"), gst=Decimal("1500.00"), label="big purchase")

    amounts = await _gst101_box_vector(
        company_id, from_date=date(2026, 7, 1), to_date=date(2026, 8, 31)
    )
    assert amounts["8"] == Decimal("300.00")
    assert amounts["12"] == Decimal("1500.00")
    assert amounts["15"] == Decimal("-1200.00")


async def test_nz_gst101_manual_adjustment_boxes_flow_into_totals() -> None:
    """Boxes 9/13 (calculation-sheet adjustments) are manual: absent =
    explicit 0; supplied via manual_values they flow into Boxes 10/14/15
    — the same mechanic as EE KMD's boxes 4-1/10/11."""
    company_id = await _make_nz_company()
    accounts, tax_by_type = await _company_fixtures(company_id)
    await _post_payable_period(company_id, accounts, tax_by_type, date(2026, 9, 15))

    amounts = await _gst101_box_vector(
        company_id,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 10, 31),
        manual_values={"9": Decimal("100.00"), "13": Decimal("50.00")},
    )
    assert amounts["9"] == Decimal("100.00")
    assert amounts["10"] == Decimal("1600.00")
    assert amounts["13"] == Decimal("50.00")
    assert amounts["14"] == Decimal("500.00")
    assert amounts["15"] == Decimal("1100.00")
