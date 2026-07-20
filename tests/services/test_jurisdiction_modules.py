"""Jurisdiction-module Phase 0 seam tests.

Covers the three new pieces the bolt-on contract introduced (design doc
``jurisdiction-module-architecture-design.md`` §§2-3):

* ``payroll.get_payroll_engine`` — the new per-capability registry
  (AU → the wrapping engine; "XX"/unregistered → the neutral null
  object, never a raise).
* ``tax_engine`` neutral floor — the reserved "XX" sentinel resolves to
  ``NeutralTaxEngine`` via ``get_engine``; ``resolve_engine`` degrades
  UNREGISTERED codes to neutral too (the posting-path contract), while
  ``get_engine`` stays strict (KeyError) for them.
* ``jurisdiction_modules`` — descriptor catalogue + registration entry
  point.

Pure in-memory — no DB. The AU byte-identity of the payroll seam is
proven by the EXISTING pay-run suites (test_pay_runs_v2*, unchanged
expectations); the record-only "zero modules" behaviour end-to-end is
tests/test_zero_modules_xx.py.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from saebooks.jurisdictions.au.payroll import AUPayrollEngine
from saebooks.models.account import AccountType
from saebooks.services import jurisdiction_modules as jm
from saebooks.services.payroll import (
    NEUTRAL_POSTING_PROFILE,
    NeutralPayrollEngine,
    PayrollComponentRole,
    PayrollContext,
    PayrollEngine,
    get_payroll_engine,
    get_posting_profile,
)
from saebooks.services.tax_engine import (
    NEUTRAL_JURISDICTION,
    PostingContext,
    get_engine,
    resolve_engine,
)
from saebooks.services.tax_engine.neutral import NeutralTaxEngine


def test_get_payroll_engine_au_returns_au_engine() -> None:
    engine = get_payroll_engine("AU")
    assert isinstance(engine, AUPayrollEngine)
    assert isinstance(engine, PayrollEngine)  # runtime-checkable Protocol
    assert engine.jurisdiction == "AU"


def test_get_payroll_engine_neutral_sentinel_and_unregistered() -> None:
    # The reserved sentinel and any unregistered jurisdiction both
    # degrade to the null object — the payroll registry never raises.
    # (NZ and UK both left this list when their jurisdiction modules
    # registered payroll engines — use codes with NO module here; the
    # next module author must swap theirs out too, per the
    # test_business_identifiers.py NOTE precedent.)
    for code in (NEUTRAL_JURISDICTION, "ZZ", "DE"):
        engine = get_payroll_engine(code)
        assert isinstance(engine, NeutralPayrollEngine), code


async def test_neutral_payroll_engine_pays_gross_no_components() -> None:
    engine = NeutralPayrollEngine()
    ctx = PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period="MONTHLY",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        effective_date=date(2026, 4, 30),
        gross=Decimal("5000.00"),
        ote=Decimal("5000.00"),
        deductions_total=Decimal("150.00"),
    )
    result = await engine.compute_line(None, ctx)
    assert result.jurisdiction == NEUTRAL_JURISDICTION
    assert result.gross == Decimal("5000.00")
    assert result.net == Decimal("4850.00")  # gross - deductions, nothing else
    assert result.components == ()


def test_get_posting_profile_au_matches_pre_phase1_je_shape() -> None:
    """Phase 1: the AU posting profile must encode the exact hardcoded
    JE shape ``finalize_with_je`` used to carry — same chart codes,
    same leg order (Dr SG / Cr PAYG WH / Cr Super payable), same line
    labels — so the posted AU journal stays byte-identical."""
    from saebooks.jurisdictions.au import PAYROLL_POSTING

    profile = get_posting_profile("AU")
    assert profile is PAYROLL_POSTING
    assert profile.wages_account_code == "6-2110"
    assert profile.wages_label == "Wages"
    assert profile.net_account_code == "2-1150"
    assert profile.net_label == "Net pay"
    assert [
        (ra.role, ra.account_code, ra.label) for ra in profile.role_accounts
    ] == [
        (PayrollComponentRole.RETIREMENT_EXPENSE, "6-2120", "SG"),
        (PayrollComponentRole.WITHHOLDING_LIABILITY, "2-1310", "PAYG WH"),
        (PayrollComponentRole.RETIREMENT_LIABILITY, "2-1320", "Super payable"),
    ]


def test_get_posting_profile_unregistered_degrades_to_neutral() -> None:
    # Same never-raise contract as get_payroll_engine: sentinel and
    # unregistered codes get the wages+net-only neutral profile.
    # (same no-module-codes rule as the engine-registry test above)
    for code in (NEUTRAL_JURISDICTION, "ZZ", "DE"):
        assert get_posting_profile(code) is NEUTRAL_POSTING_PROFILE, code
    assert NEUTRAL_POSTING_PROFILE.role_accounts == ()


def test_component_role_posting_direction() -> None:
    # Expense roles debit (employer cost legs); liability roles credit.
    assert PayrollComponentRole.RETIREMENT_EXPENSE.posts_debit
    assert PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE.posts_debit
    assert not PayrollComponentRole.WITHHOLDING_LIABILITY.posts_debit
    assert not PayrollComponentRole.RETIREMENT_LIABILITY.posts_debit
    assert not PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY.posts_debit


def test_get_engine_xx_returns_neutral_tax_engine() -> None:
    engine = get_engine(NEUTRAL_JURISDICTION)
    assert isinstance(engine, NeutralTaxEngine)
    assert engine.jurisdiction == NEUTRAL_JURISDICTION


def test_resolve_engine_degrades_unregistered_to_neutral() -> None:
    # resolve_engine (the posting path's dispatcher) never KeyErrors —
    # an unregistered code gets the neutral null object.
    assert isinstance(resolve_engine("ZZ"), NeutralTaxEngine)
    # Registered engines resolve exactly as get_engine does.
    assert resolve_engine("AU").jurisdiction == "AU"


def test_neutral_tax_engine_records_zero_tax() -> None:
    engine = NeutralTaxEngine()
    ctx = PostingContext(
        company_id=uuid.uuid4(),
        jurisdiction=NEUTRAL_JURISDICTION,
        posting_date=date(2026, 1, 15),
        account_id=uuid.uuid4(),
        account_type=AccountType.INCOME,
        amount=Decimal("1000.00"),
    )
    treatment = engine.compute(ctx)
    assert treatment.tax == Decimal("0")
    assert treatment.rate == Decimal("0")
    assert treatment.base == Decimal("1000.00")
    assert treatment.direction == "none"
    assert treatment.reporting_type == "none"
    assert engine.compute_components(ctx) == [treatment]
    assert engine.boxes(None) == {}
    assert engine.validate(None) == []


def test_descriptor_catalogue_has_au_and_neutral() -> None:
    au = jm.get_descriptor("AU")
    assert au is not None
    assert au.provides_tax and au.provides_payroll and au.provides_lodgement
    assert au.has_seed_dir
    assert au.min_edition_for_lodgement == "pro"

    xx = jm.get_descriptor(NEUTRAL_JURISDICTION)
    assert xx is not None
    assert not (xx.provides_tax or xx.provides_payroll or xx.provides_lodgement)

    codes = [d.code for d in jm.list_descriptors()]
    assert codes == sorted(codes)
    assert "AU" in codes and NEUTRAL_JURISDICTION in codes


def test_register_jurisdiction_module_populates_registries() -> None:
    """A synthetic module registers per-capability factories and appears
    in the catalogue; capabilities it omits fall through to neutral."""
    from saebooks.services import payroll as payroll_registry
    from saebooks.services import tax_engine as tax_registry

    class _FakePayrollEngine(NeutralPayrollEngine):
        jurisdiction = "Q1"

    try:
        jm.register_jurisdiction_module(
            jm.JurisdictionModuleDescriptor(
                code="Q1",
                label="Test-only jurisdiction",
                provides_tax=False,
                provides_payroll=True,
                provides_lodgement=False,
                has_seed_dir=False,
            ),
            payroll=_FakePayrollEngine,
        )
        assert isinstance(get_payroll_engine("Q1"), _FakePayrollEngine)
        # No tax factory registered → strict get_engine raises, the
        # posting-path resolve_engine degrades to neutral.
        assert isinstance(resolve_engine("Q1"), NeutralTaxEngine)
        assert jm.get_descriptor("Q1") is not None
    finally:
        # Keep the process-global registries clean for other tests.
        payroll_registry._REGISTRY.pop("Q1", None)
        tax_registry._REGISTRY.pop("Q1", None)
        jm._DESCRIPTORS.pop("Q1", None)
