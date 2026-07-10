"""Tests for the EE tax engine (KMD-formula support Packet 3).

Pure-unit, no DB — mirrors tests/services/test_tax_engine.py's shape for
AU. Covers: ordinary EE VAT lines behave like AU's algorithm (same
direction/base/tax derivation), the reverse-charge two-component
fan-out (compute_components returns output+input for
rc_eu_acq_goods/rc_eu_acq_services, single component otherwise), the
deductible_fraction partial-deduction hook, and get_engine("EE") no
longer being a stub (also pinned in test_tax_engine.py).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.models.account import AccountType
from saebooks.services.tax_engine import PostingContext, TaxTreatment, get_engine
from saebooks.services.tax_engine.ee import (
    RC_DUAL_REPORTING_TYPES,
    EETaxEngine,
    ReverseChargeRateNotSupportedError,
)

pytestmark = pytest.mark.postgres_only


def _ctx(
    *,
    account_type: AccountType,
    amount: Decimal,
    rate: Decimal | None = None,
    gst_amount: Decimal | None = None,
    tax_code: str | None = "EE-STD",
    reporting_type: str | None = "standard",
    extra: dict | None = None,
) -> PostingContext:
    return PostingContext(
        company_id=uuid.uuid4(),
        jurisdiction="EE",
        posting_date=date(2026, 7, 15),
        account_id=uuid.uuid4(),
        account_type=account_type,
        amount=amount,
        gst_amount=gst_amount,
        tax_code=tax_code,
        rate=rate,
        reporting_type=reporting_type,
        extra=extra or {},
    )


def test_get_engine_ee_returns_eetaxengine() -> None:
    engine = get_engine("EE")
    assert isinstance(engine, EETaxEngine)
    assert engine.jurisdiction == "EE"


# ---------------------------------------------------------------------------
# Ordinary (non-reverse-charge) lines — same shape as AU's algorithm.
# ---------------------------------------------------------------------------


def test_compute_income_line_with_gst_amount_supplied() -> None:
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("10000.00"),
        rate=Decimal("24.000"),
        gst_amount=Decimal("2400.00"),
    )
    treatment = engine.compute(ctx)
    assert isinstance(treatment, TaxTreatment)
    assert treatment.jurisdiction == "EE"
    assert treatment.code == "EE-STD"
    assert treatment.base == Decimal("10000.00")
    assert treatment.tax == Decimal("2400.00")
    assert treatment.direction == "output"
    assert treatment.reporting_type == "standard"


def test_compute_expense_line_derives_tax_from_rate() -> None:
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("100.00"),
        rate=Decimal("24.000"),
        gst_amount=None,
    )
    treatment = engine.compute(ctx)
    assert treatment.direction == "input"
    assert treatment.base == Decimal("100.00")
    assert treatment.tax == Decimal("24.00")


def test_compute_equity_line_has_no_direction() -> None:
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EQUITY,
        amount=Decimal("1000.00"),
        rate=None,
        gst_amount=None,
        tax_code=None,
        reporting_type=None,
    )
    treatment = engine.compute(ctx)
    assert treatment.direction == "none"
    assert treatment.tax == Decimal("0")


def test_compute_components_single_element_for_ordinary_line() -> None:
    """A non-reverse-charge line yields exactly one component — same
    shape as AU's compute_components (the dispatcher's default path)."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("1000.00"),
        rate=Decimal("24.000"),
        gst_amount=Decimal("240.00"),
    )
    treatments = engine.compute_components(ctx)
    assert len(treatments) == 1
    assert treatments[0] == engine.compute(ctx)


def test_validate_returns_empty_list() -> None:
    assert EETaxEngine().validate(object()) == []


def test_boxes_not_implemented() -> None:
    """Mirrors AU's own au.py note: bas_report (generate_return for AU),
    not AUTaxEngine.boxes, is the production reporting path — same for
    EE via generate_return(jurisdiction='EE', return_type='KMD')."""
    with pytest.raises(NotImplementedError, match="generate_return"):
        EETaxEngine().boxes(object())


# ---------------------------------------------------------------------------
# Reverse-charge two-component fan-out (scope §3.4 points 2/3).
# ---------------------------------------------------------------------------


def test_rc_dual_reporting_types_are_exactly_eu_acquisition_tags() -> None:
    assert RC_DUAL_REPORTING_TYPES == {"rc_eu_acq_goods", "rc_eu_acq_services"}


@pytest.mark.parametrize("reporting_type", ["rc_eu_acq_goods", "rc_eu_acq_services"])
def test_compute_components_reverse_charge_yields_output_and_input(reporting_type: str) -> None:
    """An EU-acquisition purchase line yields BOTH an output-role and an
    input-role component, same base, same tax (full deduction — the
    default) — the scope's §6 'balanced' proof: output == input."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("4000.00"),
        rate=Decimal("24.000"),
        gst_amount=Decimal("960.00"),
        tax_code="RC-EUACQ",
        reporting_type=reporting_type,
    )
    treatments = engine.compute_components(ctx)
    assert len(treatments) == 2

    output, input_ = treatments
    assert output.direction == "output"
    assert input_.direction == "input"
    assert output.base == input_.base == Decimal("4000.00")
    assert output.tax == input_.tax == Decimal("960.00")
    assert output.reporting_type == input_.reporting_type == reporting_type
    assert output.code == input_.code == "RC-EUACQ"
    assert "reverse_charge_output" in output.notes
    assert "reverse_charge_input" in input_.notes


def test_compute_reverse_charge_primary_treatment_is_output() -> None:
    """compute() (the single-treatment entry point, used for the
    journal_lines.tax_treatment JSONB snapshot) returns the FIRST
    compute_components element — the output-role component for a
    reverse-charge line."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("4000.00"),
        rate=Decimal("24.000"),
        gst_amount=Decimal("960.00"),
        reporting_type="rc_eu_acq_goods",
    )
    treatment = engine.compute(ctx)
    assert treatment.direction == "output"
    assert treatment.tax == Decimal("960.00")


def test_compute_reverse_charge_partial_deduction_scales_only_input() -> None:
    """§30 partial-deduction hook: deductible_fraction scales ONLY the
    input-role component — the output-role self-assessed liability is
    never partial."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("4000.00"),
        rate=Decimal("24.000"),
        gst_amount=Decimal("960.00"),
        reporting_type="rc_eu_acq_goods",
        extra={"deductible_fraction": Decimal("0.5")},
    )
    output, input_ = engine.compute_components(ctx)
    assert output.tax == Decimal("960.00")
    assert input_.tax == Decimal("480.00")


def test_compute_reverse_charge_derives_tax_from_rate_when_gst_amount_none() -> None:
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("4000.00"),
        rate=Decimal("24.000"),
        gst_amount=None,
        reporting_type="rc_eu_acq_goods",
    )
    output, input_ = engine.compute_components(ctx)
    assert output.tax == input_.tax == Decimal("960.00")


# ---------------------------------------------------------------------------
# Finding 1 (rate-aware RC routing): the KMD seed wires per-rate RC legs
# for the three current positive-rate output boxes — 24% (box 1), 9%
# (box 2) and 13% (box 2-2). Those three rates are now ACCEPTED and routed
# by rate; a genuinely-unsupported vintage (20%/22% legacy standard, 5%)
# with no output box wired for it is still rejected loudly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reporting_type", ["rc_eu_acq_goods", "rc_eu_acq_services"])
@pytest.mark.parametrize("rate", [Decimal("20.000"), Decimal("22.000"), Decimal("5.000")])
def test_compute_reverse_charge_rejects_unsupported_rate(
    reporting_type: str, rate: Decimal
) -> None:
    """A legacy-standard (20%/22%) or unwired (5%) reverse-charge rate has
    no KMD output box — posting it would land the base in no output box
    while box 5 still deducted the input VAT, so it must raise loudly
    rather than silently mis-report the return."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("1000.00"),
        rate=rate,
        gst_amount=(Decimal("1000.00") * rate / Decimal("100")),
        reporting_type=reporting_type,
    )
    with pytest.raises(ReverseChargeRateNotSupportedError):
        engine.compute_components(ctx)


@pytest.mark.parametrize("reporting_type", ["rc_eu_acq_goods", "rc_eu_acq_services"])
@pytest.mark.parametrize("rate", [Decimal("24.000"), Decimal("9.000"), Decimal("13.000")])
def test_compute_reverse_charge_accepts_all_wired_rates(
    reporting_type: str, rate: Decimal
) -> None:
    """Each of the three wired rates (24/9/13) yields the normal
    output+input component pair with the rate carried through — the base
    for box 1/2/2-2 routing, the tax for box 5."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("1000.00"),
        rate=rate,
        gst_amount=(Decimal("1000.00") * rate / Decimal("100")),
        reporting_type=reporting_type,
    )
    treatments = engine.compute_components(ctx)
    assert [t.direction for t in treatments] == ["output", "input"]
    assert all(t.rate == rate for t in treatments)


def test_compute_reverse_charge_derives_tax_from_rate_when_gst_amount_none() -> None:
    """Finding 6 (derive path): an RC line posted without an explicit tax
    amount (gst_amount=None — the natural shape of a foreign supplier's
    VAT-free invoice) DERIVES the self-assessed VAT from the rate, so
    BOTH components carry a non-zero tax — never a silent zero while the
    snapshot claims a taxable acquisition. The complementary fail-loud
    path (a rate with no KMD box) is
    test_compute_reverse_charge_rejects_unsupported_rate."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("4000.00"),
        rate=Decimal("24.000"),
        gst_amount=None,
        reporting_type="rc_eu_acq_goods",
    )
    output, input_ = engine.compute_components(ctx)
    assert output.tax == input_.tax == Decimal("960.00")  # 4000 * 24% derived
    assert output.base == input_.base == Decimal("4000.00")


def test_compute_reverse_charge_accepts_supported_rate_still() -> None:
    """The current standard rate (24%) still yields the normal
    output+input pair — the guard doesn't regress the supported path."""
    engine = EETaxEngine()
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("1000.00"),
        rate=Decimal("24.000"),
        gst_amount=Decimal("240.00"),
        reporting_type="rc_eu_acq_goods",
    )
    output, input_ = engine.compute_components(ctx)
    assert output.tax == input_.tax == Decimal("240.00")
