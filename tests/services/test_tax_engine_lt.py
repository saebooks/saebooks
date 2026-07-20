"""LTTaxEngine unit tests — PVM determination + the reverse-charge
two-component fan-out (engine-direct; the dispatch-path pin lives in
test_jurisdiction_lt_registration.py and the FR0600 golden drives the
full posting path)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.jurisdictions.lt.tax import (
    RC_DUAL_REPORTING_TYPES,
    LTTaxEngine,
)
from saebooks.models.account import AccountType
from saebooks.services.tax_engine.types import PostingContext


def _ctx(**overrides) -> PostingContext:
    defaults = dict(
        company_id=uuid.uuid4(),
        jurisdiction="LT",
        posting_date=date(2026, 5, 15),
        account_id=uuid.uuid4(),
        account_type=AccountType.INCOME,
        amount=Decimal("1000.00"),
        rate=Decimal("21.000"),
        tax_code="STD",
        reporting_type="standard",
    )
    defaults.update(overrides)
    return PostingContext(**defaults)


def test_standard_sale_21_percent() -> None:
    t = LTTaxEngine().compute(_ctx())
    assert t.jurisdiction == "LT"
    assert t.direction == "output"
    assert t.tax == Decimal("210.00")
    assert t.base == Decimal("1000.00")


def test_reduced_12_percent_derives_from_rate() -> None:
    t = LTTaxEngine().compute(
        _ctx(rate=Decimal("12.000"), tax_code="RED12", reporting_type="reduced_12")
    )
    assert t.tax == Decimal("120.00")


def test_caller_supplied_gst_amount_wins() -> None:
    t = LTTaxEngine().compute(_ctx(gst_amount=Decimal("209.99")))
    assert t.tax == Decimal("209.99")


def test_purchase_line_is_input_direction() -> None:
    t = LTTaxEngine().compute(
        _ctx(account_type=AccountType.EXPENSE, reporting_type="standard")
    )
    assert t.direction == "input"


def test_ordinary_line_single_component() -> None:
    components = LTTaxEngine().compute_components(_ctx())
    assert len(components) == 1


@pytest.mark.parametrize("tag", sorted(RC_DUAL_REPORTING_TYPES))
def test_reverse_charge_fans_out_output_plus_input(tag: str) -> None:
    components = LTTaxEngine().compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            reporting_type=tag,
            amount=Decimal("5000.00"),
        )
    )
    assert [c.direction for c in components] == ["output", "input"]
    output, input_c = components
    assert output.tax == input_c.tax == Decimal("1050.00")
    assert output.base == input_c.base == Decimal("5000.00")
    assert "reverse_charge_output" in output.notes
    assert "reverse_charge_input" in input_c.notes
    # compute() snapshots the FIRST (output-role) component.
    first = LTTaxEngine().compute(
        _ctx(account_type=AccountType.EXPENSE, reporting_type=tag)
    )
    assert first.direction == "output"


def test_reverse_charge_any_rate_routes_no_refusal() -> None:
    """FR0600's self-assessment boxes are role-keyed, not rate-split
    (the UK VAT100 posture) — a non-21% reverse charge computes fine,
    unlike EE's KMD rate refusal."""
    components = LTTaxEngine().compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            reporting_type="rc_eu_acq_goods",
            rate=Decimal("12.000"),
            amount=Decimal("100.00"),
        )
    )
    assert [c.tax for c in components] == [Decimal("12.00"), Decimal("12.00")]


def test_deductible_fraction_scales_input_leg_only() -> None:
    components = LTTaxEngine().compute_components(
        _ctx(
            account_type=AccountType.EXPENSE,
            reporting_type="rc_eu_acq_services",
            amount=Decimal("1000.00"),
            extra={"deductible_fraction": "0.5"},
        )
    )
    output, input_c = components
    assert output.tax == Decimal("210.00")   # liability never partial
    assert input_c.tax == Decimal("105.00")  # reclaim capped


def test_domestic_rc_supply_is_not_fanned_out() -> None:
    """The SELLER side of the Art 96 reverse charge is a plain
    zero-VAT sale (box 12) — the customer self-assesses on their own
    ledger."""
    components = LTTaxEngine().compute_components(
        _ctx(
            reporting_type="rc_domestic_supply",
            rate=Decimal("0.000"),
            tax_code="RC_DOM_SUPPLY",
        )
    )
    assert len(components) == 1
    assert components[0].tax == Decimal("0")


def test_boxes_protocol_method_points_at_fr0600_report() -> None:
    with pytest.raises(NotImplementedError, match="fr0600_report"):
        LTTaxEngine().boxes(None)
