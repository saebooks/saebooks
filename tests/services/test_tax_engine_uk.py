"""Tests for the UK tax engine (UK jurisdiction module).

Pure-unit (no DB): UKTaxEngine's standard single-component compute and
the reverse-charge / PVA two-component fan-out — mirrors
tests/services/test_tax_engine_ee.py's shape. The end-to-end posting +
VAT100 box-vector proof lives in
tests/services/test_uk_vat100_golden.py.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from saebooks.jurisdictions.uk.tax import (
    RC_DUAL_REPORTING_TYPES,
    UKTaxEngine,
)
from saebooks.models.account import AccountType
from saebooks.services.tax_engine.types import PostingContext


def _ctx(
    *,
    account_type: AccountType,
    amount: str,
    rate: str | None = None,
    gst_amount: str | None = None,
    reporting_type: str | None = None,
    tax_code: str | None = None,
    extra: dict | None = None,
) -> PostingContext:
    return PostingContext(
        company_id=uuid.uuid4(),
        jurisdiction="UK",
        posting_date=date(2026, 5, 15),
        account_id=uuid.uuid4(),
        account_type=account_type,
        amount=Decimal(amount),
        rate=None if rate is None else Decimal(rate),
        gst_amount=None if gst_amount is None else Decimal(gst_amount),
        reporting_type=reporting_type,
        tax_code=tax_code,
        extra=extra or {},
    )


def test_standard_sale_single_output_component() -> None:
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.INCOME,
            amount="1000.00",
            rate="20",
            gst_amount="200.00",
            reporting_type="standard",
            tax_code="STD",
        )
    )
    assert len(components) == 1
    t = components[0]
    assert t.jurisdiction == "UK"
    assert t.direction == "output"
    assert t.base == Decimal("1000.00")
    assert t.tax == Decimal("200.00")
    assert t.reporting_type == "standard"


def test_reduced_purchase_derives_tax_from_rate_when_gst_absent() -> None:
    engine = UKTaxEngine()
    t = engine.compute(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="240.00",
            rate="5",
            reporting_type="reduced",
        )
    )
    assert t.direction == "input"
    assert t.tax == Decimal("12.00")


def test_exempt_sale_zero_tax() -> None:
    engine = UKTaxEngine()
    t = engine.compute(
        _ctx(
            account_type=AccountType.INCOME,
            amount="500.00",
            rate="0",
            reporting_type="exempt",
        )
    )
    assert t.tax == Decimal("0")
    assert t.direction == "output"


def test_rc_tags_cover_the_seeded_convention() -> None:
    assert RC_DUAL_REPORTING_TYPES == {
        "rc_construction",
        "rc_services_intl",
        "pva_import",
        "xi_eu_acq_goods",
    }


def test_reverse_charge_construction_fans_out_output_and_input() -> None:
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="2000.00",
            rate="20",
            gst_amount="400.00",
            reporting_type="rc_construction",
            tax_code="RC_CONSTRUCTION",
        )
    )
    assert len(components) == 2
    output, input_component = components
    assert output.direction == "output"
    assert output.notes == ("reverse_charge_output",)
    assert output.tax == Decimal("400.00")
    assert output.base == Decimal("2000.00")
    assert input_component.direction == "input"
    assert input_component.notes == ("reverse_charge_input",)
    assert input_component.tax == Decimal("400.00")
    assert input_component.base == Decimal("2000.00")
    # compute() returns the FIRST (output) component — the snapshot
    # convention services.journal._apply_tax_treatment relies on.
    assert engine.compute(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="2000.00",
            rate="20",
            gst_amount="400.00",
            reporting_type="rc_construction",
        )
    ).direction == "output"


def test_pva_import_fans_out_and_derives_from_rate() -> None:
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="5000.00",
            rate="20",
            reporting_type="pva_import",
        )
    )
    assert [c.direction for c in components] == ["output", "input"]
    assert components[0].tax == Decimal("1000.00")
    assert components[1].tax == Decimal("1000.00")


def test_reverse_charge_any_rate_routes_no_rate_refusal() -> None:
    """Unlike EE's KMD (rate-split output boxes -> unsupported rates are
    rejected), VAT100 box 1 takes all rates — a 5% DRC line computes
    fine."""
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="1000.00",
            rate="5",
            reporting_type="rc_construction",
        )
    )
    assert components[0].tax == Decimal("50.00")
    assert components[1].tax == Decimal("50.00")


def test_reverse_charge_partial_deduction_scales_input_only() -> None:
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="1000.00",
            rate="20",
            gst_amount="200.00",
            reporting_type="rc_services_intl",
            extra={"deductible_fraction": "0.5"},
        )
    )
    assert components[0].tax == Decimal("200.00")   # output never partial
    assert components[1].tax == Decimal("100.00")   # input scaled


def test_xi_acquisition_fans_out() -> None:
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            amount="1500.00",
            rate="20",
            gst_amount="300.00",
            reporting_type="xi_eu_acq_goods",
        )
    )
    assert [c.direction for c in components] == ["output", "input"]


def test_seller_side_drc_supply_is_single_component() -> None:
    """rc_construction_supply (the SELLER of a DRC service) is a plain
    zero-rated sale — no fan-out (the customer self-assesses on their
    own ledger)."""
    engine = UKTaxEngine()
    components = engine.compute_components(
        _ctx(
            account_type=AccountType.INCOME,
            amount="2000.00",
            rate="0",
            reporting_type="rc_construction_supply",
        )
    )
    assert len(components) == 1
    assert components[0].tax == Decimal("0")
