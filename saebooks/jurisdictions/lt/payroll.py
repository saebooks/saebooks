"""LT payroll compute — progressive GPM with the income-dependent NPD,
employee Sodra with the 60-VDU VSD ceiling, optional II-pillar
accumulation, employer Sodra — behind the neutral ``PayrollEngine``
seam.

LT jurisdiction module. Calendar-year-2026 values; every figure is a
lock-step snapshot of this module's seed files
(``seeds/jurisdictions/LT/withholding_tables.yaml``,
``social_contribution_schemes.yaml``,
``mandatory_contribution_rules.yaml``) whose primary pulls are
documented there. Rate tables are EMBEDDED dated constants (the AU
``super_calc`` convention; reference-preferred re-sourcing is a later
phase), kept in lock-step by tests/services/test_payroll_lt.py's
seed-consistency check.

The 2026 system (Law XV-343, in force 2026-01-01)
-------------------------------------------------
* GPM: THREE progressive bands on aggregated annual income — 20% to
  36 VDU (EUR 83,237.40), 25% to 60 VDU (EUR 138,729.00), 32% above
  (VDU 2026 = EUR 2,312.15/month, the FINAL indicators-law figure).
  The engine applies the thresholds MONTH-PROPORTIONALLY (annual/12 =
  EUR 6,936.45 / 11,560.75) — the withholding-agent per-payment
  convention; the employee's annual return settles the aggregate.
  Stated approximation, same class as the UK free-pay convention note.
* NPD (neapmokestinamasis pajamų dydis) — a FORMULA, not a constant:
  monthly employment income <= MMA (EUR 1,153) -> NPD = EUR 747; above
  -> NPD = 747 - 0.49 x (income - 1,153), floored at 0 (exhausts at
  ~EUR 2,677.49). Fixed income-independent NPD for reduced working
  capacity: EUR 1,127 (0-25% participation level) / EUR 1,057
  (30-55%). GPM taxable base = gross - NPD (Sodra contributions do NOT
  reduce the GPM base).
* Employee Sodra 19.5%: pension 8.72 + sickness 1.99 + maternity 1.81
  (together the 12.52% "VSD", capped at 60 VDU = EUR 138,729/year via
  the caller-supplied YTD base) + health PSD 6.98% (NO ceiling).
* II pillar (pensijų kaupimas): VOLUNTARY from 2026 (auto-enrolment
  abolished); participants contribute 3% of gross (may elect higher),
  withheld through payroll; the state incentive is not an employer
  cost. NO employer component — contrast AU SG / UK auto-enrolment.
* Employer Sodra 1.77% standard: unemployment 1.31 (fixed-term
  contracts 2.03 -> 2.49% total) + accident 0.14 (risk Group I) +
  Guarantee Fund 0.16 + Long-term Employment Benefit Fund 0.16 —
  uncapped (employer contributions have not capped since 2021).

LT statutory inputs ride in ``ctx.extra["lt"]`` (overriding
``employee.extra["lt"]`` — the NZ convention; the ``Employee`` model
has no LT columns and adding them is a schema change this build must
not make)::

    {
        "apply_npd": True,                # REQUIRED — NPD applies only on the
                                          # employee's request at ONE workplace
        "disability_npd": "severe",       # optional: "severe" (0-25%) | "moderate" (30-55%)
        "pillar_ii": True,                # optional, default False (voluntary from 2026)
        "pillar_ii_rate_percent": 3,      # optional, default 3, must be >= 3
        "fixed_term": False,              # optional — employer unemployment 2.03%
        "accident_risk_group": "I",       # optional, default "I" (only Group I verified)
        "ytd_sodra_base": "0.00",         # optional — VSD-ceiling income to date
                                          # EXCLUDING this period
    }

Hard refusals — never a silent wrong number (the EE/NZ/UK convention):
a missing ``extra["lt"]`` block or ``apply_npd`` key, pay periods other
than MONTHLY (the NPD formula and MMA are monthly statutory
quantities; LT payroll is a monthly cycle), payment dates before
2026-01-01 (the pre-2026 20%/32% + two-formula-NPD system is not
implemented), accident risk groups other than I, II-pillar rates below
the statutory 3%, an unknown ``disability_npd`` value, and a II-pillar
computation in a period where the VSD ceiling binds (the
ceiling-vs-accumulation interaction is UNVERIFIED — refuse rather than
guess) all raise :class:`LTPayrollUnsupported`.

Role mapping (why II pillar rides WITHHOLDING, and the employer-social
schema gap): the pay-run line schema persists exactly two statutory
columns (``tax`` <- WITHHOLDING_LIABILITY, ``super_amount`` <-
RETIREMENT_LIABILITY) and ``finalize_with_je`` reconstructs
RETIREMENT_EXPENSE as a mirror of ``super_amount`` — an
employee-funded amount in the RETIREMENT pair would fabricate a
phantom employer expense. GPM, employee Sodra and the II-pillar
deduction are all carved out of gross, so all are
WITHHOLDING_LIABILITY components (separate components, distinct audit
notes; one summed control account 4460, with the VMI/Sodra remittance
split done off the notes — the NZ/UK one-control-account shape). The
employer Sodra pair (EMPLOYER_SOCIAL_LIABILITY/EXPENSE) is computed
and returned correctly here, and the LT posting profile maps it, but
the v2 pay-run line schema has no column to persist it between compute
and finalize — the SAME pre-existing gap the NZ/UK builds flagged
(schema extension named in the build report; no alembic in this
build).

Balance proof (what the core posts from this result):
    Dr wages gross + Dr employer_sodra
        = Cr (gpm + vsd + psd + pillar_ii) + Cr employer_sodra + Cr net
    with net = gross - gpm - vsd - psd - pillar_ii - deductions_total. ✓
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from saebooks.jurisdictions.lt import (
    identifiers as _identifiers,  # noqa: F401  (registers the lt_* validators on first LT dispatch)
)
from saebooks.money import round_money
from saebooks.services.payroll.types import (
    PayrollComponent,
    PayrollComponentRole,
    PayrollContext,
    PayrollResult,
)

# --------------------------------------------------------------------- #
# Errors                                                                #
# --------------------------------------------------------------------- #


class LTPayrollError(ValueError):
    """Base — LT payroll compute cannot proceed."""


class LTPayrollUnsupported(LTPayrollError):
    """A case the LT engine deliberately refuses (EE convention:
    a clear error, never a silent wrong number)."""


# --------------------------------------------------------------------- #
# Embedded 2026 parameter snapshot (lock-step with the LT seeds)        #
# --------------------------------------------------------------------- #

_ZERO = Decimal("0")
_CENT = Decimal("0.01")

#: Engine floor — the 2026 three-band + single-NPD system only.
_SUPPORTED_FROM = date(2026, 1, 1)

#: VDU for tax purposes 2026 (the FINAL 2026 indicators-law figure —
#: NOT the 2,304.50 projection from mid-2025 circulars).
VDU_MONTHLY = Decimal("2312.15")

# GPM progressive bands — ANNUAL aggregated-income thresholds
# (36 VDU / 60 VDU); applied month-proportionally below.
_GPM_BANDS: tuple[tuple[Decimal, Decimal | None, Decimal], ...] = (
    (_ZERO, Decimal("83237.40"), Decimal("20")),
    (Decimal("83237.40"), Decimal("138729.00"), Decimal("25")),
    (Decimal("138729.00"), None, Decimal("32")),
)

# NPD 2026 (GPMĮ Art. 20, one formula).
_NPD_BASE = Decimal("747.00")
_MMA_MONTHLY = Decimal("1153.00")
_NPD_TAPER = Decimal("0.49")
_NPD_DISABILITY: dict[str, Decimal] = {
    "severe": Decimal("1127.00"),    # 0-25% participation level
    "moderate": Decimal("1057.00"),  # 30-55% participation level
}

# Employee Sodra.
_VSD_EMPLOYEE_PERCENT = Decimal("12.52")   # pension 8.72 + sickness 1.99 + maternity 1.81
_PSD_EMPLOYEE_PERCENT = Decimal("6.98")    # health — NO ceiling
_SODRA_CEILING_ANNUAL = Decimal("138729.00")  # 60 VDU; employee VSD only

# II pillar.
_PILLAR_II_MIN_PERCENT = Decimal("3")

# Employer Sodra (risk Group I).
_ER_UNEMPLOYMENT_PERCENT = Decimal("1.31")
_ER_UNEMPLOYMENT_FIXED_TERM_PERCENT = Decimal("2.03")
_ER_ACCIDENT_GROUP1_PERCENT = Decimal("0.14")
_ER_GUARANTEE_FUND_PERCENT = Decimal("0.16")
_ER_LDU_FUND_PERCENT = Decimal("0.16")


def monthly_npd(
    income_monthly: Decimal, *, disability: str | None = None
) -> Decimal:
    """The 2026 monthly NPD formula (GPMĮ Art. 20) — public so GPM313
    artefact builders and tests can call it directly.

    Income-dependent: <= MMA -> EUR 747; above -> 747 - 0.49 x
    (income - 1,153), floored at 0. ``disability`` short-circuits to
    the fixed income-independent amounts.
    """
    if disability is not None:
        try:
            return _NPD_DISABILITY[disability]
        except KeyError:
            raise LTPayrollUnsupported(
                f"Unknown disability_npd value {disability!r} — supported: "
                f"{sorted(_NPD_DISABILITY)} (0-25% / 30-55% participation "
                "level). Refusing to guess a fixed-NPD tier."
            ) from None
    if income_monthly <= _MMA_MONTHLY:
        return _NPD_BASE
    tapered = _NPD_BASE - _NPD_TAPER * (income_monthly - _MMA_MONTHLY)
    return max(_ZERO, tapered.quantize(_CENT))


def _monthly_gpm(taxable_monthly: Decimal) -> Decimal:
    """Progressive GPM on a monthly taxable base, using the annual
    band thresholds divided by 12 (EUR 6,936.45 / 11,560.75) — the
    withholding-agent per-payment convention (module docstring)."""
    tax = _ZERO
    for lower_a, upper_a, rate in _GPM_BANDS:
        lower = (lower_a / 12).quantize(_CENT)
        upper = None if upper_a is None else (upper_a / 12).quantize(_CENT)
        if taxable_monthly <= lower:
            break
        top = taxable_monthly if upper is None else min(taxable_monthly, upper)
        tax += (top - lower) * rate / Decimal("100")
    return round_money(tax)


def _lt_inputs(ctx: PayrollContext) -> dict[str, Any]:
    """The engine's per-employee statutory inputs — ``ctx.extra['lt']``
    (direct callers/tests) overriding ``employee.extra['lt']`` (the
    stored convention). Missing entirely = hard refusal."""
    if ctx.extra and isinstance(ctx.extra.get("lt"), dict):
        return ctx.extra["lt"]
    employee_extra = getattr(ctx.employee, "extra", None)
    if isinstance(employee_extra, dict) and isinstance(employee_extra.get("lt"), dict):
        return employee_extra["lt"]
    raise LTPayrollUnsupported(
        "LT payroll requires the employee's LT statutory details at "
        "employee.extra['lt'] (or PayrollContext.extra['lt']) — at "
        "minimum {'apply_npd': bool}. The Employee model has no LT "
        "columns yet (schema change deferred); refusing to guess "
        "whether the NPD applies."
    )


class LTPayrollEngine:
    """Progressive GPM + NPD + Sodra (employee/employer) + optional
    II pillar behind the neutral seam.

    Embedded dated constants — ``session`` is accepted per the
    ``PayrollEngine`` protocol but not used (no reference-DB reads in
    this phase; re-sourcing to ``withholding_tables`` with embedded
    fallback is the later phase, same as AU's SG-rate plan).
    """

    jurisdiction = "LT"

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        if ctx.effective_date < _SUPPORTED_FROM:
            raise LTPayrollUnsupported(
                f"LT payroll compute supports payment dates from "
                f"{_SUPPORTED_FROM.isoformat()} (the Law XV-343 three-band "
                f"GPM + single-NPD system); got "
                f"{ctx.effective_date.isoformat()} — the pre-2026 20%/32% "
                "system and two-formula NPD are not implemented."
            )
        if ctx.period != "MONTHLY":
            raise LTPayrollUnsupported(
                f"LT payroll supports the MONTHLY pay period only — the "
                f"NPD formula and MMA are monthly statutory quantities; "
                f"got {ctx.period!r}."
            )
        if ctx.gross < 0:
            raise LTPayrollError("gross must be non-negative")

        inputs = _lt_inputs(ctx)
        if "apply_npd" not in inputs:
            raise LTPayrollUnsupported(
                "extra['lt']['apply_npd'] is required — the NPD applies "
                "only on the employee's request at ONE workplace, and "
                "guessing changes the withholding. Set it explicitly."
            )

        gross = round_money(Decimal(str(ctx.gross)))

        # ------ NPD + GPM ------------------------------------------------
        npd = _ZERO
        npd_note = "NPD not applied (not requested at this workplace)"
        if inputs["apply_npd"]:
            disability = inputs.get("disability_npd")
            npd = monthly_npd(gross, disability=disability)
            if disability is not None:
                npd_note = (
                    f"fixed disability NPD ({disability}) EUR {npd}"
                )
            elif gross <= _MMA_MONTHLY:
                npd_note = f"NPD EUR {npd} (income <= MMA {_MMA_MONTHLY})"
            else:
                npd_note = (
                    f"NPD EUR {npd} = 747 - 0.49 x ({gross} - "
                    f"{_MMA_MONTHLY}), floored at 0"
                )
        taxable = max(_ZERO, gross - npd)
        gpm = _monthly_gpm(taxable)

        # ------ Employee Sodra (VSD capped, PSD uncapped) -----------------
        ytd_base = round_money(Decimal(str(inputs.get("ytd_sodra_base", "0"))))
        if ytd_base < 0:
            raise LTPayrollError("ytd_sodra_base must be non-negative")
        ceiling_remaining = max(_ZERO, _SODRA_CEILING_ANNUAL - ytd_base)
        vsd_base = min(gross, ceiling_remaining)
        vsd = round_money(vsd_base * _VSD_EMPLOYEE_PERCENT / Decimal("100"))
        psd = round_money(gross * _PSD_EMPLOYEE_PERCENT / Decimal("100"))

        # ------ II pillar (voluntary from 2026) ---------------------------
        pillar_ii = _ZERO
        pillar_note = ""
        if inputs.get("pillar_ii"):
            if vsd_base < gross:
                raise LTPayrollUnsupported(
                    "the 60-VDU Sodra ceiling binds in this period "
                    f"(ytd_sodra_base {ytd_base} + gross {gross} exceeds "
                    f"EUR {_SODRA_CEILING_ANNUAL}) and the ceiling's "
                    "interaction with the II-pillar accumulation "
                    "contribution is UNVERIFIED — refusing to guess the "
                    "contribution base."
                )
            raw_rate = inputs.get("pillar_ii_rate_percent", _PILLAR_II_MIN_PERCENT)
            rate = Decimal(str(raw_rate))
            if rate < _PILLAR_II_MIN_PERCENT:
                raise LTPayrollUnsupported(
                    f"II-pillar rate {rate}% is below the statutory "
                    f"participant contribution of {_PILLAR_II_MIN_PERCENT}% "
                    "(participants may elect a HIGHER rate, not lower; "
                    "suspension is modelled as pillar_ii=False)."
                )
            pillar_ii = round_money(gross * rate / Decimal("100"))
            pillar_note = (
                f"II-pillar accumulation {rate}% of gross {gross} = "
                f"EUR {pillar_ii} (voluntary participation, withheld with "
                "Sodra; the ~1.5%-of-average-wage state incentive is not "
                "an employer cost)"
            )

        # ------ Employer Sodra --------------------------------------------
        if str(inputs.get("accident_risk_group", "I")).upper() != "I":
            raise LTPayrollUnsupported(
                "only accident/occupational-disease risk Group I "
                f"({_ER_ACCIDENT_GROUP1_PERCENT}%) carries a verified 2026 "
                "rate — higher-risk groups are seeded structurally only. "
                "Refusing to compute an unverified employer rate."
            )
        unemployment_pct = (
            _ER_UNEMPLOYMENT_FIXED_TERM_PERCENT
            if inputs.get("fixed_term")
            else _ER_UNEMPLOYMENT_PERCENT
        )
        employer_pct = (
            unemployment_pct
            + _ER_ACCIDENT_GROUP1_PERCENT
            + _ER_GUARANTEE_FUND_PERCENT
            + _ER_LDU_FUND_PERCENT
        )
        employer_sodra = round_money(gross * employer_pct / Decimal("100"))
        employer_note = (
            f"Employer Sodra {employer_pct}% of gross {gross} = "
            f"EUR {employer_sodra} (unemployment {unemployment_pct}"
            f"{' fixed-term' if inputs.get('fixed_term') else ''} + "
            f"accident Group I {_ER_ACCIDENT_GROUP1_PERCENT} + Guarantee "
            f"Fund {_ER_GUARANTEE_FUND_PERCENT} + Long-term Employment "
            f"Benefit Fund {_ER_LDU_FUND_PERCENT}; uncapped)"
        )

        net = round_money(
            gross - gpm - vsd - psd - pillar_ii - ctx.deductions_total
        )

        components: list[PayrollComponent] = [
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=gpm,
                note=(
                    f"GPM (progressive 20/25/32, monthly thresholds "
                    f"{(Decimal('83237.40') / 12).quantize(_CENT)}/"
                    f"{(Decimal('138729.00') / 12).quantize(_CENT)}) on "
                    f"taxable {taxable}; {npd_note} — remit to VMI "
                    "(GPM313, 15th)"
                ),
            ),
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=vsd,
                note=(
                    f"Employee Sodra VSD {_VSD_EMPLOYEE_PERCENT}% "
                    f"(pension 8.72 + sickness 1.99 + maternity 1.81) on "
                    f"base {vsd_base}"
                    + (
                        f" (60-VDU ceiling: YTD {ytd_base} of "
                        f"{_SODRA_CEILING_ANNUAL})"
                        if vsd_base < gross
                        else ""
                    )
                    + " — remit to Sodra (SAM, 15th)"
                ),
            ),
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=psd,
                note=(
                    f"Employee Sodra PSD (health) {_PSD_EMPLOYEE_PERCENT}% "
                    f"of gross {gross} — NO ceiling; remit to Sodra"
                ),
            ),
        ]
        if pillar_ii > 0:
            components.append(
                PayrollComponent(
                    role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                    amount=pillar_ii,
                    note=pillar_note,
                )
            )
        components.append(
            PayrollComponent(
                role=PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY,
                amount=employer_sodra,
                note=employer_note,
            )
        )
        components.append(
            PayrollComponent(
                role=PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE,
                amount=employer_sodra,
                note=employer_note,
            )
        )

        return PayrollResult(
            jurisdiction="LT",
            gross=gross,
            net=net,
            components=tuple(components),
        )


__all__ = [
    "LTPayrollEngine",
    "LTPayrollError",
    "LTPayrollUnsupported",
    "VDU_MONTHLY",
    "monthly_npd",
]
