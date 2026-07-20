"""NZ payroll engine — compute cases, refusals, registration, seed lock-step.

Compute expectations are hand-derived from the verified §5.4 facts
(annualise → brackets → ACC levy → de-annualise):

* Weekly $1,500 M, 2026-04-15 (2026-27 year): annual 78,000 →
  income tax 15,620.50 (15,600x10.5% + 37,900x17.5% + 24,500x30%),
  levy 78,000 x 1.75% = 1,365 → (15,620.50+1,365)/52 = **326.64**.
* Same on 2026-03-15 (2025-26 year, levy 1.67%): (15,620.50 +
  1,302.60)/52 = **325.44** — the 2026-04-01 boundary test.
* Student loan (M SL, weekly): threshold 24,128/52 = 464.00 →
  (1,500-464) x 12% = **124.32**.
* KiwiSaver on 2026-04-15: default employee 3.5% → 52.50; employer min
  3.5% → 52.50 gross; ESCT band on annualised 78,000 + 2,730 = 80,730
  → 30% band → ESCT 15.75. On 2026-03-15 the defaults are 3%/3%.
* Secondary SB weekly $300: 300x10.5% + 300x1.75% = **36.75**.
* Levy cap: weekly $4,000 (annual 208,000 > 156,641 cap) → levy
  156,641x1.75%/52 = 52.72; tax 60,177.50/52 = 1,157.64 → **1,210.36**.

Every statutory input rides in ``employee.extra['nz']`` (the engine's
documented convention — the Employee model has no NZ columns yet).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from saebooks.jurisdictions.nz.payroll import (
    NZPayrollEngine,
    NZPayrollUnsupported,
    esct_rate_for,
    kiwisaver_rates_for,
)
from saebooks.services.payroll.types import (
    PayrollComponentRole,
    PayrollContext,
)

WH = PayrollComponentRole.WITHHOLDING_LIABILITY
RETL = PayrollComponentRole.RETIREMENT_LIABILITY
RETE = PayrollComponentRole.RETIREMENT_EXPENSE


def _ctx(
    *,
    nz: dict | None,
    gross: str = "1500.00",
    period: str = "WEEKLY",
    effective_date: date = date(2026, 4, 15),
    deductions: str = "0",
) -> PayrollContext:
    employee = SimpleNamespace(extra={"nz": nz} if nz is not None else {})
    return PayrollContext(
        company_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        pay_run_id=uuid.uuid4(),
        period=period,
        period_start=date(2026, 4, 6),
        period_end=date(2026, 4, 12),
        effective_date=effective_date,
        gross=Decimal(gross),
        ote=Decimal(gross),
        deductions_total=Decimal(deductions),
        employee=employee,
    )


async def _compute(ctx: PayrollContext):
    return await NZPayrollEngine().compute_line(None, ctx)


# ---------------------------------------------------------------------------
# PAYE — M code, secondary codes, the 2026-04-01 boundary, levy cap.
# ---------------------------------------------------------------------------


async def test_paye_m_code_weekly() -> None:
    r = await _compute(_ctx(nz={"tax_code": "M"}))
    assert r.jurisdiction == "NZ"
    assert r.total_for(WH) == Decimal("326.64")
    assert r.net == Decimal("1173.36")
    assert r.total_for(RETL) == Decimal("0")     # not a KiwiSaver member
    assert "ACC earner's levy 1.75%" in r.note_for(WH)


async def test_paye_rate_boundary_2026_04_01() -> None:
    # Same gross, one day either side of the ACC-levy year change:
    # 1.67%/$152,790 (2025-26) vs 1.75%/$156,641 (2026-27).
    before = await _compute(_ctx(nz={"tax_code": "M"}, effective_date=date(2026, 3, 31)))
    after = await _compute(_ctx(nz={"tax_code": "M"}, effective_date=date(2026, 4, 1)))
    assert before.total_for(WH) == Decimal("325.44")
    assert after.total_for(WH) == Decimal("326.64")


async def test_paye_levy_cap_binds_for_high_earner() -> None:
    r = await _compute(_ctx(nz={"tax_code": "M"}, gross="4000.00"))
    # annual 208,000: tax 60,177.50; levy capped at 156,641 x 1.75%.
    assert r.total_for(WH) == Decimal("1210.36")


async def test_paye_secondary_sb_flat_rate_plus_levy() -> None:
    r = await _compute(_ctx(nz={"tax_code": "SB"}, gross="300.00"))
    assert r.total_for(WH) == Decimal("36.75")


async def test_paye_monthly_period() -> None:
    r = await _compute(_ctx(nz={"tax_code": "M"}, gross="8000.00", period="MONTHLY"))
    # annual 96,000: tax 21,557.50; levy 96,000 x 1.75% = 1,680.
    assert r.total_for(WH) == Decimal("1936.46")


# ---------------------------------------------------------------------------
# Student loan.
# ---------------------------------------------------------------------------


async def test_student_loan_primary_threshold() -> None:
    r = await _compute(_ctx(nz={"tax_code": "M SL"}))
    sl = [c for c in r.components if c.role is WH and "Student loan" in c.note]
    assert len(sl) == 1
    assert sl[0].amount == Decimal("124.32")   # (1,500 - 24,128/52) x 12%
    assert r.total_for(WH) == Decimal("326.64") + Decimal("124.32")
    assert r.net == Decimal("1500.00") - Decimal("326.64") - Decimal("124.32")


async def test_student_loan_below_threshold_is_zero() -> None:
    r = await _compute(_ctx(nz={"tax_code": "M SL"}, gross="400.00"))
    assert not [c for c in r.components if "Student loan" in c.note]


async def test_student_loan_secondary_no_threshold() -> None:
    r = await _compute(_ctx(nz={"tax_code": "SB SL"}, gross="300.00"))
    sl = [c for c in r.components if "Student loan" in c.note]
    assert sl[0].amount == Decimal("36.00")    # 12% of the whole secondary income


# ---------------------------------------------------------------------------
# KiwiSaver + ESCT.
# ---------------------------------------------------------------------------


async def test_kiwisaver_default_rates_step_at_2026_04_01() -> None:
    before = await _compute(
        _ctx(nz={"tax_code": "M", "kiwisaver_member": True}, effective_date=date(2026, 3, 31))
    )
    after = await _compute(
        _ctx(nz={"tax_code": "M", "kiwisaver_member": True}, effective_date=date(2026, 4, 1))
    )
    # Employee deduction (a WH component): 3% -> 3.5% of $1,500.
    assert [c.amount for c in before.components if "KiwiSaver employee" in c.note] == [Decimal("45.00")]
    assert [c.amount for c in after.components if "KiwiSaver employee" in c.note] == [Decimal("52.50")]
    # Employer minimum steps the same way (the RETIREMENT pair).
    assert before.total_for(RETL) == Decimal("45.00")
    assert after.total_for(RETL) == Decimal("52.50")
    # Expense mirrors liability (the pay-run line schema's contract).
    assert after.total_for(RETE) == after.total_for(RETL)


async def test_kiwisaver_2028_step_to_four_percent() -> None:
    # The dated table carries the legislated 2028 step...
    rates = kiwisaver_rates_for(date(2028, 4, 1))
    assert rates.default_employee_percent == Decimal("4")
    assert rates.employer_min_percent == Decimal("4")
    # ...but a FULL pay-line compute on 2028-04-01 must still refuse:
    # the 2028-29 ACC earner's levy is not yet published/verified
    # (_ACC_LEVY_HISTORY ends 2028-03-31), and a KiwiSaver-right,
    # levy-wrong payslip would be a silent wrong number.
    with pytest.raises(NZPayrollUnsupported, match="ACC earner's levy"):
        await _compute(
            _ctx(nz={"tax_code": "M", "kiwisaver_member": True}, effective_date=date(2028, 4, 1))
        )


async def test_kiwisaver_elected_higher_rate_and_esct_note() -> None:
    r = await _compute(
        _ctx(nz={"tax_code": "M", "kiwisaver_member": True, "kiwisaver_employee_rate_percent": 6})
    )
    ee = [c for c in r.components if "KiwiSaver employee" in c.note]
    assert ee[0].amount == Decimal("90.00")        # 6% employee
    assert r.total_for(RETL) == Decimal("52.50")   # employer stays at the 3.5% minimum
    # ESCT snapshot lives in the employer component note (band 30% on
    # annualised 78,000 + 2,730 = 80,730): 52.50 x 30% = 15.75.
    note = r.note_for(RETL)
    assert "ESCT 30.00%" in note and "$15.75" in note


async def test_kiwisaver_out_of_set_rate_refused() -> None:
    with pytest.raises(NZPayrollUnsupported, match="allowed set"):
        await _compute(
            _ctx(nz={"tax_code": "M", "kiwisaver_member": True, "kiwisaver_employee_rate_percent": 5})
        )


async def test_kiwisaver_employer_below_minimum_refused() -> None:
    with pytest.raises(NZPayrollUnsupported, match="statutory minimum"):
        await _compute(
            _ctx(nz={"tax_code": "M", "kiwisaver_member": True, "kiwisaver_employer_rate_percent": 3})
        )


async def test_kiwisaver_temporary_opt_down_employer_may_match_at_three() -> None:
    r = await _compute(
        _ctx(nz={
            "tax_code": "M",
            "kiwisaver_member": True,
            "kiwisaver_employee_rate_percent": 3,
            "kiwisaver_employer_rate_percent": 3,
        })
    )
    assert r.total_for(RETL) == Decimal("45.00")


async def test_esct_band_selection() -> None:
    assert esct_rate_for(Decimal("18720")) == Decimal("0.105")
    assert esct_rate_for(Decimal("18720.01")) == Decimal("0.175")
    assert esct_rate_for(Decimal("64200.01")) == Decimal("0.30")
    assert esct_rate_for(Decimal("93720.01")) == Decimal("0.33")
    assert esct_rate_for(Decimal("216000.01")) == Decimal("0.39")


async def test_esct_employer_determined_rate_override() -> None:
    r = await _compute(
        _ctx(nz={
            "tax_code": "M",
            "kiwisaver_member": True,
            "esct_rate_percent": 39,
        })
    )
    note = r.note_for(RETL)
    assert "ESCT 39.00%" in note and "employer-determined" in note


# ---------------------------------------------------------------------------
# Hard refusals (never a silent wrong number).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["WT", "STC", "ME", "CAE", "EDW", "NSW"])
async def test_known_unsupported_codes_hard_refuse(code: str) -> None:
    with pytest.raises(NZPayrollUnsupported, match="not supported"):
        await _compute(_ctx(nz={"tax_code": code}))


async def test_unknown_code_refused_with_supported_list() -> None:
    with pytest.raises(NZPayrollUnsupported, match="Unknown NZ tax code"):
        await _compute(_ctx(nz={"tax_code": "QQ"}))


async def test_missing_nz_inputs_refused() -> None:
    with pytest.raises(NZPayrollUnsupported, match="extra\\['nz'\\]"):
        await _compute(_ctx(nz=None))


async def test_missing_tax_code_refused() -> None:
    with pytest.raises(NZPayrollUnsupported, match="tax_code"):
        await _compute(_ctx(nz={"kiwisaver_member": True}))


async def test_pre_supported_range_refused() -> None:
    with pytest.raises(NZPayrollUnsupported, match="2025-04-01"):
        await _compute(_ctx(nz={"tax_code": "M"}, effective_date=date(2025, 3, 31)))


async def test_unsupported_period_refused() -> None:
    with pytest.raises(NZPayrollUnsupported, match="WEEKLY / FORTNIGHTLY / MONTHLY"):
        await _compute(_ctx(nz={"tax_code": "M"}, period="QUARTERLY"))


async def test_ctx_extra_overrides_employee_extra() -> None:
    ctx = _ctx(nz={"tax_code": "WT"})
    override = PayrollContext(
        **{
            **{f.name: getattr(ctx, f.name) for f in ctx.__dataclass_fields__.values()},  # type: ignore[attr-defined]
            "extra": {"nz": {"tax_code": "M"}},
        }
    )
    r = await NZPayrollEngine().compute_line(None, override)
    assert r.total_for(WH) == Decimal("326.64")


# ---------------------------------------------------------------------------
# Registration + posting profile (the module is bolted on).
# ---------------------------------------------------------------------------


def test_nz_module_registered() -> None:
    from saebooks.jurisdictions.nz import PAYROLL_POSTING
    from saebooks.services import jurisdiction_modules as jm
    from saebooks.services.payroll import get_payroll_engine, get_posting_profile

    d = jm.get_descriptor("NZ")
    assert d is not None
    assert (d.provides_tax, d.provides_payroll, d.provides_lodgement) == (True, True, True)
    assert d.has_seed_dir is True
    assert isinstance(get_payroll_engine("NZ"), NZPayrollEngine)
    assert get_posting_profile("NZ") is PAYROLL_POSTING


def test_nz_posting_profile_shape() -> None:
    from saebooks.jurisdictions.nz import PAYROLL_POSTING

    assert PAYROLL_POSTING.wages_account_code == "6-2110"
    assert PAYROLL_POSTING.net_account_code == "2-1150"
    assert [
        (ra.role, ra.account_code) for ra in PAYROLL_POSTING.role_accounts
    ] == [
        (RETE, "6-2120"),
        (WH, "2-1310"),
        (RETL, "2-1320"),
    ]


async def test_result_balances_under_the_core_je_shape() -> None:
    # The core posts: Dr gross + Dr RETE == Cr WH + Cr RETL + Cr net.
    r = await _compute(
        _ctx(nz={"tax_code": "M SL", "kiwisaver_member": True}, deductions="25.00")
    )
    debits = r.gross + r.total_for(RETE)
    credits = r.total_for(WH) + r.total_for(RETL) + r.net + Decimal("25.00")
    assert debits == credits


# ---------------------------------------------------------------------------
# Seed lock-step — the embedded tables and the NZ seed YAML must agree.
# ---------------------------------------------------------------------------

_NZ_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "NZ"
)


def test_embedded_tables_match_seed_yaml() -> None:
    from saebooks.jurisdictions.nz import payroll as nzp

    wt = yaml.safe_load((_NZ_DIR / "withholding_tables.yaml").read_text())
    paye_rows = sorted(
        (r for r in wt["rows"] if r["code"] == "nz_paye"),
        key=lambda r: r["effective_from"],
    )
    assert len(paye_rows) == len(nzp._ACC_LEVY_HISTORY)
    for row, band in zip(paye_rows, nzp._ACC_LEVY_HISTORY, strict=True):
        levy = row["parameters"]["acc_earners_levy"]
        assert Decimal(str(levy["rate_percent"])) / 100 == band.rate
        assert Decimal(str(levy["earnings_cap"])) == band.earnings_cap
        assert row["effective_from"] == band.effective_from
        # Bracket table identical across rows and equal to the embedded set.
        brackets = row["parameters"]["brackets"]
        assert [
            (
                Decimal(str(b["lower"])),
                None if b["upper"] is None else Decimal(str(b["upper"])),
                Decimal(str(b["rate_percent"])) / 100,
            )
            for b in brackets
        ] == list(nzp._BRACKETS)

    esct_rows = [r for r in wt["rows"] if r["code"] == "nz_esct"]
    assert len(esct_rows) == 1
    seed_bands = [
        (
            None if b["upper"] is None else Decimal(str(b["upper"])),
            Decimal(str(b["rate_percent"])) / 100,
        )
        for b in esct_rows[0]["parameters"]["bands"]
    ]
    assert seed_bands == list(nzp._ESCT_BANDS)

    sl_rows = [r for r in wt["rows"] if r["code"] == "nz_student_loan"]
    assert Decimal(str(sl_rows[0]["parameters"]["rate_percent"])) / 100 == nzp._SL_RATE
    assert (
        Decimal(str(sl_rows[0]["parameters"]["annual_repayment_threshold"]))
        == nzp._SL_ANNUAL_THRESHOLD
    )

    mcr = yaml.safe_load((_NZ_DIR / "mandatory_contribution_rules.yaml").read_text())
    ee_steps = sorted(
        (
            (r["effective_from"], Decimal(str(r["rate_percent"])))
            for r in mcr["rows"]
            if r["code"] == "nz_kiwisaver_employee_default"
        ),
    )
    er_steps = sorted(
        (
            (r["effective_from"], Decimal(str(r["rate_percent"])))
            for r in mcr["rows"]
            if r["code"] == "nz_kiwisaver_employer_min"
        ),
    )
    embedded = [
        (b.effective_from, b.default_employee_percent, b.employer_min_percent)
        for b in nzp._KS_HISTORY
    ]
    # The seed's first row starts 2013 (historical); the engine's floor
    # is 2025-04-01 — rates must agree from the engine floor onward.
    assert [(d, ee) for d, ee, _er in embedded] == [
        (max(d, nzp._SUPPORTED_FROM), r) for d, r in ee_steps
    ]
    assert [(d, er) for d, _ee, er in embedded] == [
        (max(d, nzp._SUPPORTED_FROM), r) for d, r in er_steps
    ]
