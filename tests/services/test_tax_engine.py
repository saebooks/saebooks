"""Tests for the tax_engine package and the AU implementation."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.jurisdictions.au.tax import AUTaxEngine
from saebooks.models.account import AccountType
from saebooks.services.tax_engine import (
    PostingContext,
    TaxTreatment,
    get_engine,
)

pytestmark = pytest.mark.postgres_only


def _ctx(
    *,
    account_type: AccountType,
    amount: Decimal,
    rate: Decimal | None = None,
    gst_amount: Decimal | None = None,
    tax_code: str | None = "GST",
    reporting_type: str | None = "taxable",
) -> PostingContext:
    return PostingContext(
        company_id=uuid.uuid4(),
        jurisdiction="AU",
        posting_date=date(2026, 1, 15),
        account_id=uuid.uuid4(),
        account_type=account_type,
        amount=amount,
        gst_amount=gst_amount,
        tax_code=tax_code,
        rate=rate,
        reporting_type=reporting_type,
    )


def test_get_engine_au_returns_autaxengine() -> None:
    engine = get_engine("AU")
    assert isinstance(engine, AUTaxEngine)
    assert engine.jurisdiction == "AU"


def test_get_engine_unknown_raises_keyerror() -> None:
    # "XX" is no longer the unknown-code probe — it is the RESERVED
    # neutral sentinel (jurisdiction-module Phase 0) and resolves to
    # NeutralTaxEngine. "ZZ" (also ISO user-assigned, never a real
    # country) takes over as the genuinely-unregistered code here.
    with pytest.raises(KeyError, match="Unknown jurisdiction"):
        get_engine("ZZ")


def test_get_engine_nz_returns_nztaxengine() -> None:
    # Behaviour change (NZ jurisdiction module, flagged): NZ used to be
    # an unbuilt stub (raised NotImplementedError, "M1") — the NZ module
    # landed the real engine via the same in-file lazy factory shape as
    # AU. No seed/caller ever relied on NZ raising (the harness even
    # --ignore'd the old synthetic-NZ M0 test), so this is the same safe
    # tightening EE's Packet 3 flip was.
    from saebooks.jurisdictions.nz.tax import NZTaxEngine

    engine = get_engine("NZ")
    assert isinstance(engine, NZTaxEngine)
    assert engine.jurisdiction == "NZ"


def test_get_engine_uk_returns_uktaxengine() -> None:
    """Behaviour change (UK jurisdiction module, flagged): UK used to be
    an unbuilt stub (raised NotImplementedError, "M2") — the UK module
    landed the real engine (jurisdictions.uk.tax.UKTaxEngine), the same
    flagged flip EE made below when Packet 3 landed EETaxEngine. No
    seed/caller ever relied on UK raising."""
    engine = get_engine("UK")
    assert type(engine).__name__ == "UKTaxEngine"
    assert engine.jurisdiction == "UK"


def test_get_engine_ee_returns_eetaxengine() -> None:
    """Behaviour change (KMD-formula support Packet 3, flagged): EE used
    to be an unbuilt stub (raised NotImplementedError, "M3") — Packet 3
    landed the real engine (services.tax_engine.ee.EETaxEngine), the
    prerequisite for the reverse-charge fan-out. No seed/caller ever
    relied on EE raising, so this is a safe tightening, not a behaviour
    change for real data (mirrors Packet 1's own flagged change to the
    legacy inline formula-prefix test)."""
    from saebooks.services.tax_engine.ee import EETaxEngine

    engine = get_engine("EE")
    assert isinstance(engine, EETaxEngine)
    assert engine.jurisdiction == "EE"


def test_compute_income_line_with_gst_amount_supplied() -> None:
    """Sales line with GST already split out — engine trusts the caller."""
    engine = get_engine("AU")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("100.00"),
        rate=Decimal("0.10"),
        gst_amount=Decimal("10.00"),
    )
    treatment = engine.compute(ctx)
    assert isinstance(treatment, TaxTreatment)
    assert treatment.jurisdiction == "AU"
    assert treatment.code == "GST"
    assert treatment.rate == Decimal("0.10")
    assert treatment.base == Decimal("100.00")
    assert treatment.tax == Decimal("10.00")
    assert treatment.reporting_type == "taxable"
    assert treatment.direction == "output"


def test_compute_expense_line_derives_tax_from_rate() -> None:
    """Purchase line with no pre-computed gst_amount — engine derives it."""
    engine = get_engine("AU")
    ctx = _ctx(
        account_type=AccountType.EXPENSE,
        amount=Decimal("200.00"),
        rate=Decimal("0.10"),
        gst_amount=None,
    )
    treatment = engine.compute(ctx)
    assert treatment.direction == "input"
    assert treatment.base == Decimal("200.00")
    assert treatment.tax == Decimal("20.00")


def test_compute_zero_rate_line_yields_zero_tax() -> None:
    engine = get_engine("AU")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("50.00"),
        rate=Decimal("0"),
        gst_amount=None,
        reporting_type="gst_free",
        tax_code="FRE",
    )
    treatment = engine.compute(ctx)
    assert treatment.tax == Decimal("0")
    assert treatment.rate == Decimal("0")
    assert treatment.reporting_type == "gst_free"
    assert treatment.code == "FRE"


def test_compute_equity_line_has_no_direction() -> None:
    engine = get_engine("AU")
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


def test_compute_is_deterministic() -> None:
    """Same input → same output, every time."""
    engine = get_engine("AU")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("123.45"),
        rate=Decimal("0.10"),
        gst_amount=Decimal("12.35"),
    )
    a = engine.compute(ctx)
    b = engine.compute(ctx)
    assert a == b


def test_treatment_to_jsonable_round_trip() -> None:
    engine = get_engine("AU")
    ctx = _ctx(
        account_type=AccountType.INCOME,
        amount=Decimal("100.00"),
        rate=Decimal("0.10"),
        gst_amount=Decimal("10.00"),
    )
    treatment = engine.compute(ctx)
    payload = treatment.to_jsonable()
    assert payload["jurisdiction"] == "AU"
    assert payload["code"] == "GST"
    assert payload["rate"] == "0.10"
    assert payload["base"] == "100.00"
    assert payload["tax"] == "10.00"
    assert payload["direction"] == "output"
    assert payload["reporting_type"] == "taxable"
    assert payload["notes"] == []


def test_validate_returns_empty_list() -> None:
    engine = get_engine("AU")
    assert engine.validate(object()) == []


def test_legacy_gst_shim_still_imports_and_warns() -> None:
    """jurisdictions.au.gst is a deprecated shim — must still expose the public API."""
    import importlib

    import saebooks.jurisdictions.au.gst as gst_module

    with pytest.warns(DeprecationWarning, match="saebooks.jurisdictions.au.gst"):
        importlib.reload(gst_module)

    # Public names still resolve (re-exported from jurisdictions.au.tax).
    assert hasattr(gst_module, "auto_post_gst_lines")
    assert hasattr(gst_module, "is_auto_post_enabled")
    assert hasattr(gst_module, "settle_bas")


def test_legacy_bas_shim_still_imports_and_warns() -> None:
    """jurisdictions.au.bas is a deprecated shim — must still expose the public API."""
    import importlib

    import saebooks.jurisdictions.au.bas as bas_module

    with pytest.warns(DeprecationWarning, match="saebooks.jurisdictions.au.bas"):
        importlib.reload(bas_module)

    assert hasattr(bas_module, "BASLine")
    assert hasattr(bas_module, "BASReport")
    assert hasattr(bas_module, "bas_report")
