"""LV tax engine unit tests — PVN determination, the reverse-charge
fan-out routed to Latvia's dedicated rows, the loud refusals, and the
distributed-profits UIN arithmetic helper (both regimes).

Pure-unit (no DB): the engine is instantiated directly — the registry
dispatch pin lives in tests/services/test_jurisdiction_lv_registration
.py, and the posted-ledger golden in tests/services/test_lv_pvn_golden
.py.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.jurisdictions.lv.tax import (
    UIN_EVENT_DEEMED,
    UIN_EVENT_DIVIDEND,
    UIN_REGIME_ALT_INDIVIDUAL,
    UIN_REGIME_STANDARD,
    DomesticReverseChargeNotSupportedError,
    LVCorporateTaxUnsupported,
    LVTaxEngine,
    ReverseChargeRateNotSupportedError,
    compute_uin_on_distribution,
)
from saebooks.models.account import AccountType
from saebooks.services.tax_engine.types import PostingContext


def _pc(**kw) -> PostingContext:
    d = dict(
        company_id=uuid.uuid4(),
        jurisdiction="LV",
        posting_date=date(2026, 3, 15),
        account_id=uuid.uuid4(),
        account_type=AccountType.INCOME,
        amount=Decimal("100.00"),
        tax_code="STD",
        rate=Decimal("21.000"),
        reporting_type="standard",
    )
    d.update(kw)
    return PostingContext(**d)


# ---------------------------------------------------------------------------
# Ordinary determination.
# ---------------------------------------------------------------------------


def test_lv_compute_derives_21_percent_from_percentage_points() -> None:
    t = LVTaxEngine().compute(_pc())
    assert t.jurisdiction == "LV"
    assert t.tax == Decimal("21.00")  # 100 * 21 / 100 — the EE/NZ convention
    assert t.direction == "output"
    assert t.reporting_type == "standard"


def test_lv_compute_reduced_rates() -> None:
    t12 = LVTaxEngine().compute(_pc(rate=Decimal("12.000"), reporting_type="reduced_12"))
    assert t12.tax == Decimal("12.00")
    t5 = LVTaxEngine().compute(_pc(rate=Decimal("5.000"), reporting_type="reduced_5"))
    assert t5.tax == Decimal("5.00")


def test_lv_compute_trusts_caller_supplied_gst_amount() -> None:
    t = LVTaxEngine().compute(_pc(gst_amount=Decimal("20.37")))
    assert t.tax == Decimal("20.37")


def test_lv_compute_purchase_side_is_input_direction() -> None:
    t = LVTaxEngine().compute(_pc(account_type=AccountType.EXPENSE))
    assert t.direction == "input"


def test_lv_ordinary_line_is_single_component() -> None:
    comps = LVTaxEngine().compute_components(_pc())
    assert len(comps) == 1


# ---------------------------------------------------------------------------
# Reverse-charge fan-out.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reporting_type",
    ["rc_eu_acq_goods", "rc_eu_acq_services", "rc_third_country_services"],
)
def test_lv_reverse_charge_fans_out_output_plus_input(reporting_type: str) -> None:
    comps = LVTaxEngine().compute_components(
        _pc(
            account_type=AccountType.EXPENSE,
            amount=Decimal("2000.00"),
            tax_code="RC",
            rate=Decimal("21.000"),
            reporting_type=reporting_type,
        )
    )
    assert [c.direction for c in comps] == ["output", "input"]
    assert comps[0].tax == Decimal("420.00")
    assert comps[1].tax == Decimal("420.00")
    assert comps[0].base == comps[1].base == Decimal("2000.00")
    assert "reverse_charge_output" in comps[0].notes
    assert "reverse_charge_input" in comps[1].notes


def test_lv_reverse_charge_deductible_fraction_scales_input_only() -> None:
    comps = LVTaxEngine().compute_components(
        _pc(
            account_type=AccountType.EXPENSE,
            amount=Decimal("1000.00"),
            rate=Decimal("21.000"),
            reporting_type="rc_eu_acq_goods",
            extra={"deductible_fraction": "0.5"},
        )
    )
    assert comps[0].tax == Decimal("210.00")   # output liability never partial
    assert comps[1].tax == Decimal("105.00")   # input credit scaled


def test_lv_reverse_charge_unwired_rate_refused() -> None:
    with pytest.raises(ReverseChargeRateNotSupportedError, match="50/51/51.1"):
        LVTaxEngine().compute_components(
            _pc(
                account_type=AccountType.EXPENSE,
                rate=Decimal("20.000"),  # not an LV rate — no row wired
                reporting_type="rc_eu_acq_goods",
            )
        )


def test_lv_buyer_side_domestic_reverse_charge_refused() -> None:
    """The parked slice, stated loudly: rc_domestic_acq's output-side
    declaration row was not primary-verified — the engine must refuse,
    never guess a row."""
    with pytest.raises(DomesticReverseChargeNotSupportedError, match="row 62"):
        LVTaxEngine().compute_components(
            _pc(
                account_type=AccountType.EXPENSE,
                rate=Decimal("21.000"),
                reporting_type="rc_domestic_acq",
            )
        )


def test_lv_seller_side_domestic_reverse_charge_is_plain_zero_vat_sale() -> None:
    comps = LVTaxEngine().compute_components(
        _pc(rate=Decimal("0.000"), tax_code="RC_DOM_SUPPLY",
            reporting_type="rc_domestic_supply")
    )
    assert len(comps) == 1
    assert comps[0].tax == Decimal("0")
    assert comps[0].direction == "output"


# ---------------------------------------------------------------------------
# UIN — the distributed-profits arithmetic (both regimes).
# ---------------------------------------------------------------------------


def test_uin_standard_regime_is_20_on_base_divided_by_0_8() -> None:
    r = compute_uin_on_distribution(Decimal("8000.00"))
    assert r.taxable_base == Decimal("10000.00")   # 8000 / 0.8
    assert r.cit == Decimal("2000.00")             # == 25% of net — never 20% of net
    assert r.iin_withheld == Decimal("0.00")       # single-layer taxation
    assert r.total_tax == Decimal("2000.00")


def test_uin_alternative_regime_is_15_on_base_divided_by_0_85_plus_6_iin() -> None:
    r = compute_uin_on_distribution(
        Decimal("8500.00"), regime=UIN_REGIME_ALT_INDIVIDUAL
    )
    assert r.taxable_base == Decimal("10000.00")   # 8500 / 0.85
    assert r.cit == Decimal("1500.00")
    assert r.iin_withheld == Decimal("510.00")     # 6% of the net dividend
    assert r.total_tax == Decimal("2010.00")


def test_uin_alternative_regime_rounding_case() -> None:
    r = compute_uin_on_distribution(
        Decimal("10000.00"), regime=UIN_REGIME_ALT_INDIVIDUAL
    )
    assert r.taxable_base == Decimal("11764.71")
    assert r.cit == Decimal("1764.71")
    assert r.iin_withheld == Decimal("600.00")


def test_uin_deemed_distribution_forced_to_standard_even_under_election() -> None:
    """The 15% rate applies ONLY to dividends (VID bulletin) — a deemed
    distribution under an active election still computes 20%/÷0.8."""
    r = compute_uin_on_distribution(
        Decimal("8000.00"),
        regime=UIN_REGIME_ALT_INDIVIDUAL,
        event=UIN_EVENT_DEEMED,
    )
    assert r.taxable_base == Decimal("10000.00")
    assert r.cit == Decimal("2000.00")
    assert r.iin_withheld == Decimal("0.00")


def test_uin_refusals() -> None:
    with pytest.raises(LVCorporateTaxUnsupported):
        compute_uin_on_distribution(Decimal("100"), regime="flat_rate_on_profit")
    with pytest.raises(LVCorporateTaxUnsupported):
        compute_uin_on_distribution(Decimal("100"), event="liquidation")
    with pytest.raises(LVCorporateTaxUnsupported):
        compute_uin_on_distribution(Decimal("-1"))
    # Sanity pins on the constants used above.
    assert UIN_REGIME_STANDARD == "distributed_profit"
    assert UIN_EVENT_DIVIDEND == "dividend"
