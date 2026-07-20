"""LTPayrollEngine — GPM band boundaries, the NPD formula, the Sodra
ceiling, II pillar, employer rates, refusal paths, the JE balance
invariant, and the seed lock-step consistency check.

All expectations hand-computed from the 2026 primary-verified values
(module docstring / seed headers): GPM 20/25/32 at monthly thresholds
6,936.45 / 11,560.75 (annual 83,237.40 / 138,729.00 over 12); NPD
747 taper 0.49 from MMA 1,153; employee VSD 12.52% (ceiling
138,729/yr) + PSD 6.98% (uncapped); employer 1.77% / 2.49% fixed-term.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from saebooks.jurisdictions.lt.payroll import (
    LTPayrollEngine,
    LTPayrollUnsupported,
    monthly_npd,
)
from saebooks.services.payroll.types import (
    PayrollComponentRole,
    PayrollContext,
)

_SEED_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "LT"
)


def _ctx(gross: str, lt: dict | None = None, **overrides) -> PayrollContext:
    defaults = dict(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period="MONTHLY",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        effective_date=date(2026, 5, 31),
        gross=Decimal(gross),
        ote=Decimal(gross),
        deductions_total=Decimal("0"),
        extra={"lt": {"apply_npd": True, **(lt or {})}},
    )
    defaults.update(overrides)
    return PayrollContext(**defaults)


async def _run(ctx: PayrollContext):
    return await LTPayrollEngine().compute_line(None, ctx)


def _total(result, role: PayrollComponentRole) -> Decimal:
    return result.total_for(role)


# --------------------------------------------------------------------- #
# NPD formula                                                           #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("income", "expected"),
    [
        ("1000.00", "747.00"),   # <= MMA -> full base NPD
        ("1153.00", "747.00"),   # exactly MMA (boundary, inclusive)
        ("1153.01", "747.00"),   # one cent over: 747 - 0.49*0.01 = 746.9951 -> 747.00
        ("2000.00", "331.97"),   # 747 - 0.49*847
        ("2677.49", "0.00"),     # exhaustion point (taper hits zero)
        ("5000.00", "0.00"),     # far past exhaustion — floored
    ],
)
def test_npd_formula(income: str, expected: str) -> None:
    assert monthly_npd(Decimal(income)) == Decimal(expected)


def test_npd_disability_tiers_fixed_and_income_independent() -> None:
    assert monthly_npd(Decimal("9000"), disability="severe") == Decimal("1127.00")
    assert monthly_npd(Decimal("9000"), disability="moderate") == Decimal("1057.00")
    with pytest.raises(LTPayrollUnsupported, match="disability_npd"):
        monthly_npd(Decimal("9000"), disability="group_iii")


# --------------------------------------------------------------------- #
# GPM bands + full-line cases                                           #
# --------------------------------------------------------------------- #


async def test_minimum_wage_case() -> None:
    """Gross 1,000 (<= MMA): NPD 747, taxable 253, GPM 50.60;
    VSD 125.20; PSD 69.80; employer 17.70; net 754.40."""
    r = await _run(_ctx("1000.00"))
    assert _total(r, PayrollComponentRole.WITHHOLDING_LIABILITY) == Decimal(
        "50.60"
    ) + Decimal("125.20") + Decimal("69.80")
    assert _total(r, PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY) == Decimal("17.70")
    assert _total(r, PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE) == Decimal("17.70")
    assert r.net == Decimal("754.40")
    # No retirement components — the II pillar (when elected) rides
    # WITHHOLDING; there is no employer pension leg in LT.
    assert _total(r, PayrollComponentRole.RETIREMENT_LIABILITY) == Decimal("0")


async def test_npd_taper_case() -> None:
    """Gross 2,000: NPD 331.97, taxable 1,668.03, GPM 333.61 (20%);
    VSD 250.40; PSD 139.60; net 1,276.39."""
    r = await _run(_ctx("2000.00"))
    gpm = next(
        c for c in r.components
        if c.role is PayrollComponentRole.WITHHOLDING_LIABILITY and "GPM" in c.note
    )
    assert gpm.amount == Decimal("333.61")
    assert "331.97" in gpm.note  # NPD snapshot in the audit note
    assert r.net == Decimal("1276.39")


async def test_band_boundary_20_to_25() -> None:
    """Gross exactly at the first monthly threshold (6,936.45; NPD long
    exhausted): GPM = 20% flat = 1,387.29. One cent more adds 25%."""
    r_at = await _run(_ctx("6936.45"))
    gpm_at = next(c for c in r_at.components if "GPM" in c.note)
    assert gpm_at.amount == Decimal("1387.29")

    r_over = await _run(_ctx("6936.49"))
    gpm_over = next(c for c in r_over.components if "GPM" in c.note)
    # 1,387.29 + 25% x 0.04 = 1,387.30
    assert gpm_over.amount == Decimal("1387.30")


async def test_band_boundary_25_to_32_top_band() -> None:
    """Gross 12,000 (crosses 11,560.75): GPM = 1,387.29 + 25% x
    4,624.30 + 32% x 439.25 = 2,683.93 (half-up)."""
    r = await _run(_ctx("12000.00"))
    gpm = next(c for c in r.components if "GPM" in c.note)
    assert gpm.amount == Decimal("2683.93")


async def test_npd_not_applied_when_not_requested() -> None:
    """apply_npd=False: GPM on the full gross (1,000 -> 200.00)."""
    r = await _run(_ctx("1000.00", lt={"apply_npd": False}))
    gpm = next(c for c in r.components if "GPM" in c.note)
    assert gpm.amount == Decimal("200.00")


# --------------------------------------------------------------------- #
# Sodra ceiling + II pillar + employer variants                          #
# --------------------------------------------------------------------- #


async def test_sodra_ceiling_caps_vsd_not_psd() -> None:
    """YTD 138,000 of the 138,729 ceiling: VSD on the 729 remainder
    only (91.27); PSD on full gross (139.60)."""
    r = await _run(_ctx("2000.00", lt={"ytd_sodra_base": "138000.00"}))
    vsd = next(c for c in r.components if "VSD" in c.note)
    psd = next(c for c in r.components if "PSD" in c.note)
    assert vsd.amount == Decimal("91.27")
    assert psd.amount == Decimal("139.60")


async def test_pillar_ii_default_rate() -> None:
    r = await _run(_ctx("2000.00", lt={"pillar_ii": True}))
    p2 = next(c for c in r.components if "II-pillar" in c.note)
    assert p2.amount == Decimal("60.00")
    assert p2.role is PayrollComponentRole.WITHHOLDING_LIABILITY
    # Net drops by exactly the contribution vs the non-participant case.
    r_base = await _run(_ctx("2000.00"))
    assert r_base.net - r.net == Decimal("60.00")


async def test_pillar_ii_elected_higher_rate() -> None:
    r = await _run(_ctx("2000.00", lt={"pillar_ii": True, "pillar_ii_rate_percent": 6}))
    p2 = next(c for c in r.components if "II-pillar" in c.note)
    assert p2.amount == Decimal("120.00")


async def test_fixed_term_employer_rate() -> None:
    r = await _run(_ctx("2000.00", lt={"fixed_term": True}))
    assert _total(r, PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY) == Decimal("49.80")


# --------------------------------------------------------------------- #
# Refusal paths                                                         #
# --------------------------------------------------------------------- #


async def test_refuses_missing_lt_block() -> None:
    with pytest.raises(LTPayrollUnsupported, match=r"extra\['lt'\]"):
        await _run(_ctx("1000.00", extra={}))


async def test_refuses_missing_apply_npd() -> None:
    with pytest.raises(LTPayrollUnsupported, match="apply_npd"):
        await _run(_ctx("1000.00", extra={"lt": {}}))


async def test_refuses_non_monthly_period() -> None:
    with pytest.raises(LTPayrollUnsupported, match="MONTHLY"):
        await _run(_ctx("1000.00", period="WEEKLY"))


async def test_refuses_pre_2026_dates() -> None:
    with pytest.raises(LTPayrollUnsupported, match="2026-01-01"):
        await _run(_ctx("1000.00", effective_date=date(2025, 12, 31)))


async def test_refuses_non_group1_accident_risk() -> None:
    with pytest.raises(LTPayrollUnsupported, match="Group I"):
        await _run(_ctx("1000.00", lt={"accident_risk_group": "II"}))


async def test_refuses_sub_statutory_pillar_rate() -> None:
    with pytest.raises(LTPayrollUnsupported, match="3%"):
        await _run(_ctx("1000.00", lt={"pillar_ii": True, "pillar_ii_rate_percent": 2}))


async def test_refuses_pillar_ii_when_ceiling_binds() -> None:
    with pytest.raises(LTPayrollUnsupported, match="ceiling"):
        await _run(
            _ctx(
                "2000.00",
                lt={"pillar_ii": True, "ytd_sodra_base": "138000.00"},
            )
        )


# --------------------------------------------------------------------- #
# JE balance invariant + seed lock-step                                  #
# --------------------------------------------------------------------- #


async def test_line_balances_for_je_posting() -> None:
    """The invariant finalize_with_je's role-tag posting relies on:
    gross + expense components == liability components + net."""
    r = await _run(
        _ctx("3456.78", lt={"pillar_ii": True, "fixed_term": True})
    )
    debits = r.gross + sum(
        (c.amount for c in r.components if c.role.posts_debit),
        start=Decimal("0"),
    )
    credits = r.net + sum(
        (c.amount for c in r.components if not c.role.posts_debit),
        start=Decimal("0"),
    )
    assert debits == credits, f"pay-run JE would not balance: {debits} != {credits}"


def test_engine_constants_lock_step_with_seeds() -> None:
    """The embedded engine constants and the seed YAML must never
    drift (the NZ seed-consistency precedent)."""
    wh = yaml.safe_load((_SEED_DIR / "withholding_tables.yaml").read_text())
    rows = {r["code"]: r for r in wh["rows"]}
    gpm = rows["lt_gpm_progressive"]["parameters"]
    assert [b["rate_percent"] for b in gpm["brackets"]] == [20, 25, 32]
    assert Decimal(str(gpm["brackets"][0]["upper"])) == Decimal("83237.40")
    assert Decimal(str(gpm["brackets"][1]["upper"])) == Decimal("138729.00")
    assert Decimal(str(gpm["vdu_monthly"])) == Decimal("2312.15")
    npd = rows["lt_npd_monthly"]["parameters"]
    assert Decimal(str(npd["base_npd"])) == Decimal("747.00")
    assert Decimal(str(npd["mma_monthly"])) == Decimal("1153.00")
    assert Decimal(str(npd["taper_rate"])) == Decimal("0.49")
    assert Decimal(str(npd["disability_npd_severe"])) == Decimal("1127.00")
    assert Decimal(str(npd["disability_npd_moderate"])) == Decimal("1057.00")

    scs = yaml.safe_load((_SEED_DIR / "social_contribution_schemes.yaml").read_text())
    by_code = {r["code"]: r for r in scs["rows"]}
    employee_total = sum(
        Decimal(str(by_code[c]["rate_percent"]))
        for c in (
            "lt_vsd_pension_employee",
            "lt_vsd_sickness_employee",
            "lt_vsd_maternity_employee",
            "lt_psd_health_employee",
        )
    )
    assert employee_total == Decimal("19.5")
    employer_total = sum(
        Decimal(str(by_code[c]["rate_percent"]))
        for c in (
            "lt_unemployment_employer",
            "lt_accident_employer_group1",
            "lt_guarantee_fund_employer",
            "lt_ldu_fund_employer",
        )
    )
    assert employer_total == Decimal("1.77")
    fixed_term_total = employer_total - Decimal("1.31") + Decimal(
        str(by_code["lt_unemployment_er_fixed_term"]["rate_percent"])
    )
    assert fixed_term_total == Decimal("2.49")
    # Ceiling on the VSD rows only; PSD uncapped.
    assert Decimal(str(by_code["lt_vsd_pension_employee"]["wage_base_cap"])) == Decimal(
        "138729.00"
    )
    assert by_code["lt_psd_health_employee"]["wage_base_cap"] is None

    mcr = yaml.safe_load((_SEED_DIR / "mandatory_contribution_rules.yaml").read_text())
    p2 = mcr["rows"][0]
    assert p2["code"] == "lt_pillar_ii_employee"
    assert Decimal(str(p2["rate_percent"])) == Decimal("3")
    assert p2["payer"] == "employee"
