"""NZ payroll engine — PAYE + KiwiSaver + ESCT + student loan.

Jurisdiction-module bolt-on: the ``PayrollEngine`` implementation
registered for ``"NZ"`` via ``services.jurisdiction_modules`` (the AU
engine is the reference shape; EE the hard-refusal convention donor).

Source: ~/records/saebooks/nz-market-entry-strategy.md §5.4 (verified
2026-07-12; the KiwiSaver figures are the CORRECTED post-verification
values). Rate tables are EMBEDDED dated constants — the same convention
as AU ``super_calc._SG_RATE_HISTORY`` (design doc §5.4: embedded first,
reference-preferred re-sourcing is a separate later phase). The seed
copies live in ``seeds/jurisdictions/NZ/withholding_tables.yaml`` /
``mandatory_contribution_rules.yaml`` and are kept in lock-step by
tests/services/test_payroll_nz.py's seed-consistency check.

Per-employee statutory inputs
-----------------------------

The ``Employee`` model has AU columns (TFN/STSL/...) and EE columns
(``ee_pillar_ii_rate_percent``/...) but no NZ columns — adding them is
a schema change this build must not make (no alembic migrations). NZ
inputs therefore ride in the model's existing free-form ``extra`` JSONB
under an ``"nz"`` key (engine-owned convention, documented here):

    employee.extra["nz"] = {
        "tax_code": "M" | "M SL" | "SB" | "SB SL" | "S" | "SH" | "ST" | "SA" (+ " SL"),
        "kiwisaver_member": bool (default False — opted out / on savings suspension),
        "kiwisaver_employee_rate_percent": 3.5 | 4 | 6 | 8 | 10 (+ temporary 3),
        "kiwisaver_employer_rate_percent": >= the dated statutory minimum,
        "esct_rate_percent": employer-determined ESCT band rate (optional),
    }

``PayrollContext.extra["nz"]`` overrides ``employee.extra["nz"]`` when
supplied (direct callers / tests). A missing ``tax_code`` is a HARD
REFUSAL — never a silent M-code guess.

Hard refusals (the EE convention — a clear error, never a silent wrong
number): unsupported tax codes (ME — needs the IETC, whose parameters
the research pass did not verify; CAE/EDW/NSW casual-special codes;
WT schedular contractors; STC special tax codes), pay periods outside
WEEKLY/FORTNIGHTLY/MONTHLY, effective dates before 2025-04-01 (no
verified tables), and KiwiSaver rates outside the dated allowed set.

PAYE method
-----------

Annualise the period gross (x52 / x26 / x12), apply the annual bracket
table (M code) or the flat secondary rate (SB/S/SH/ST/SA), add the ACC
earner's levy on earnings up to the dated cap, de-annualise, round
half-up to the cent (``saebooks.money.round_money``). This is the
IR "formula" approach off the published annual brackets; IR's printed
pay-period tables apply their own table-step rounding, so cent-level
differences from the printed tables are possible — the annualisation
method itself and every rate/threshold are the verified facts.

Role mapping (why ESCT rides inside the RETIREMENT pair)
--------------------------------------------------------

The pay-run line schema persists exactly two statutory columns
(``tax`` <- WITHHOLDING_LIABILITY, ``super_amount`` <-
RETIREMENT_LIABILITY) and ``finalize_with_je`` reconstructs
RETIREMENT_EXPENSE as a mirror of ``super_amount``
(``pay_runs_v2._line_role_amounts``); an EMPLOYER_SOCIAL_* component
would be silently dropped between compute and finalize. So:

* PAYE (incl. earner's levy), student loan and the employee KiwiSaver
  deduction — all carved from gross, all remitted to IR together — are
  WITHHOLDING_LIABILITY components (separate components, distinct
  audit notes; one summed GL account, which IS the correct NZ shape).
* The employer KiwiSaver contribution is booked GROSS (ESCT included)
  as the RETIREMENT_LIABILITY/RETIREMENT_EXPENSE pair — expense =
  liability = full employer cost, which is true (ESCT is part of the
  employer contribution, withheld from it at remittance: net to fund,
  ESCT to IR). The ESCT amount and band are computed, snapshotted in
  the component note, and returned for payday-filing artefacts — the
  GL-level ESCT/fund split is a remittance detail the current role
  enum + line schema cannot carry losslessly (flagged as contract
  friction in the build summary, with the schema extension named).

Balance proof (what the core posts from this result):
    Dr wages gross + Dr ks_er  =  Cr (paye+sl+ks_ee) + Cr ks_er + Cr net
    with net = gross - paye - sl - ks_ee - deductions_total.  ✓
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from typing import Any

from saebooks.jurisdictions.nz import (
    identifiers as _identifiers,  # noqa: F401  (registers the nz_nzbn validator on first NZ dispatch)
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


class NZPayrollError(ValueError):
    """Base — NZ payroll compute cannot proceed."""


class NZPayrollUnsupported(NZPayrollError):
    """A case the NZ engine deliberately refuses (EE convention:
    a clear error, never a silent wrong number)."""


# --------------------------------------------------------------------- #
# Dated rate tables (embedded — super_calc convention; seed copies in   #
# seeds/jurisdictions/NZ/, lock-step-tested)                            #
# --------------------------------------------------------------------- #

#: Engine floor — nothing before the first verified tax year.
_SUPPORTED_FROM = date(2025, 4, 1)

# Individual income-tax brackets (annual NZD; 2025-26 legislated table,
# carried forward — continuity for later years is UNVERIFIED and the
# seed rows carry the same flag).
_BRACKETS: tuple[tuple[Decimal, Decimal | None, Decimal], ...] = (
    (Decimal("0"), Decimal("15600"), Decimal("0.105")),
    (Decimal("15600"), Decimal("53500"), Decimal("0.175")),
    (Decimal("53500"), Decimal("78100"), Decimal("0.30")),
    (Decimal("78100"), Decimal("180000"), Decimal("0.33")),
    (Decimal("180000"), None, Decimal("0.39")),
)

# Secondary-income flat rates by tax code.
_SECONDARY_RATES: dict[str, Decimal] = {
    "SB": Decimal("0.105"),
    "S": Decimal("0.175"),
    "SH": Decimal("0.30"),
    "ST": Decimal("0.33"),
    "SA": Decimal("0.39"),
}

_SUPPORTED_PRIMARY_CODES = frozenset({"M"})
_SUPPORTED_CODES = _SUPPORTED_PRIMARY_CODES | frozenset(_SECONDARY_RATES)

# Codes we KNOW exist and deliberately refuse (distinct message from a
# typo): ME needs the independent earner tax credit (parameters not in
# the verified research); CAE/EDW/NSW are casual/special-category codes;
# WT is schedular-payment (contractor) withholding; STC is an
# IR-issued special tax code.
_KNOWN_UNSUPPORTED_CODES = frozenset({"ME", "CAE", "EDW", "NSW", "WT", "STC"})


@dataclasses.dataclass(frozen=True)
class AccLevyBand:
    effective_from: date
    effective_to: date
    rate: Decimal          # fraction, e.g. 0.0167
    earnings_cap: Decimal  # annual NZD


# ACC earner's levy (verified §5.4 — date-effective).
_ACC_LEVY_HISTORY: tuple[AccLevyBand, ...] = (
    AccLevyBand(date(2025, 4, 1), date(2026, 3, 31), Decimal("0.0167"), Decimal("152790")),
    AccLevyBand(date(2026, 4, 1), date(2027, 3, 31), Decimal("0.0175"), Decimal("156641")),
    AccLevyBand(date(2027, 4, 1), date(2028, 3, 31), Decimal("0.0183"), Decimal("160244")),
)


@dataclasses.dataclass(frozen=True)
class KiwiSaverRates:
    effective_from: date
    default_employee_percent: Decimal
    employer_min_percent: Decimal
    allowed_employee_percent: frozenset[Decimal]


# KiwiSaver dated steps (verified §5.4 CORRECTED values): default
# employee rate and employer minimum 3% -> 3.5% on 2026-04-01 -> 4% on
# 2028-04-01; employee options 3.5/4/6/8/10 with a temporary opt-down
# to 3% (2026-2028 window; kept allowed from 2028 too because the
# opt-down mechanism itself, not its sunset, is what §5.4 verifies).
_KS_HISTORY: tuple[KiwiSaverRates, ...] = (
    KiwiSaverRates(
        date(2025, 4, 1),
        Decimal("3"), Decimal("3"),
        frozenset(Decimal(x) for x in ("3", "4", "6", "8", "10")),
    ),
    KiwiSaverRates(
        date(2026, 4, 1),
        Decimal("3.5"), Decimal("3.5"),
        frozenset(Decimal(x) for x in ("3", "3.5", "4", "6", "8", "10")),
    ),
    KiwiSaverRates(
        date(2028, 4, 1),
        Decimal("4"), Decimal("4"),
        frozenset(Decimal(x) for x in ("3", "3.5", "4", "6", "8", "10")),
    ),
)

# ESCT bands (annual ESCT rate-relevant income; 2025-26 verified — the
# competing $84,000 figure is wrong per §5.4).
_ESCT_BANDS: tuple[tuple[Decimal | None, Decimal], ...] = (
    (Decimal("18720"), Decimal("0.105")),
    (Decimal("64200"), Decimal("0.175")),
    (Decimal("93720"), Decimal("0.30")),
    (Decimal("216000"), Decimal("0.33")),
    (None, Decimal("0.39")),
)

# Student loan: 12% over the repayment threshold (annual figure verified
# for the 2026 tax year; applied across the supported range — earlier
# years are refused by the _SUPPORTED_FROM floor anyway).
_SL_RATE = Decimal("0.12")
_SL_ANNUAL_THRESHOLD = Decimal("24128")

# Pay-period annualisation factors.
_PERIODS_PER_YEAR: dict[str, Decimal] = {
    "WEEKLY": Decimal("52"),
    "FORTNIGHTLY": Decimal("26"),
    "MONTHLY": Decimal("12"),
}


# --------------------------------------------------------------------- #
# Resolvers                                                             #
# --------------------------------------------------------------------- #


def _require_supported_date(effective_date: date) -> None:
    if effective_date < _SUPPORTED_FROM:
        raise NZPayrollUnsupported(
            f"NZ payroll compute supports payment dates from "
            f"{_SUPPORTED_FROM.isoformat()} (the first verified tax "
            f"year); got {effective_date.isoformat()}."
        )


def _acc_levy_for(effective_date: date) -> AccLevyBand:
    for band in _ACC_LEVY_HISTORY:
        if band.effective_from <= effective_date <= band.effective_to:
            return band
    raise NZPayrollUnsupported(
        f"No verified ACC earner's levy rate for {effective_date.isoformat()} "
        f"(verified range {_ACC_LEVY_HISTORY[0].effective_from.isoformat()} - "
        f"{_ACC_LEVY_HISTORY[-1].effective_to.isoformat()}). Add the new "
        "year's rate/cap to _ACC_LEVY_HISTORY (and the seed) once published."
    )


def kiwisaver_rates_for(effective_date: date) -> KiwiSaverRates:
    """Dated KiwiSaver default/minimum rates (public — payday-filing
    artefact builders reuse this)."""
    chosen: KiwiSaverRates | None = None
    for band in _KS_HISTORY:
        if effective_date >= band.effective_from:
            chosen = band
    if chosen is None:
        raise NZPayrollUnsupported(
            f"No KiwiSaver rates before {_KS_HISTORY[0].effective_from.isoformat()}."
        )
    return chosen


def esct_rate_for(annual_esct_income: Decimal) -> Decimal:
    """ESCT band rate for an annual ESCT rate-relevant income figure
    (previous-year salary/wages + gross employer super contributions,
    or an estimate for a partial-year employee — s RD 67-71)."""
    for upper, rate in _ESCT_BANDS:
        if upper is None or annual_esct_income <= upper:
            return rate
    raise AssertionError("unreachable — final ESCT band is unbounded")


def _annual_income_tax(annual: Decimal) -> Decimal:
    tax = Decimal("0")
    for lower, upper, rate in _BRACKETS:
        if annual <= lower:
            break
        top = annual if upper is None else min(annual, upper)
        tax += (top - lower) * rate
    return tax


def _parse_tax_code(raw: str) -> tuple[str, bool]:
    """Split "M SL" / "SB SL" / "M" into (base_code, has_student_loan);
    hard-refuse unknown or known-unsupported codes."""
    tokens = raw.strip().upper().replace("-", " ").split()
    if not tokens:
        raise NZPayrollUnsupported("Empty NZ tax code.")
    has_sl = False
    if tokens[-1] == "SL" and len(tokens) > 1:
        has_sl = True
        tokens = tokens[:-1]
    # Also accept the compact "MSL"/"SBSL" spellings IR uses on some forms.
    base = " ".join(tokens)
    if base.endswith("SL") and base not in _SUPPORTED_CODES and base[:-2] in _SUPPORTED_CODES:
        has_sl = True
        base = base[:-2]
    if base in _KNOWN_UNSUPPORTED_CODES:
        raise NZPayrollUnsupported(
            f"NZ tax code {base!r} is not supported by this engine: "
            + (
                "ME requires the independent earner tax credit, whose "
                "parameters are not in the verified reference set."
                if base == "ME"
                else "WT schedular (contractor) payments are not wage PAYE."
                if base == "WT"
                else "STC special tax codes carry an IR-issued bespoke rate."
                if base == "STC"
                else "casual/special-category codes (CAE/EDW/NSW) are not implemented."
            )
            + " Refusing to compute a wrong number — process this employee "
            "outside SAE Books payroll or extend the NZ module."
        )
    if base not in _SUPPORTED_CODES:
        raise NZPayrollUnsupported(
            f"Unknown NZ tax code {raw!r}. Supported: "
            f"{sorted(_SUPPORTED_CODES)} (optionally with an ' SL' suffix); "
            f"known-unsupported (hard refusal): {sorted(_KNOWN_UNSUPPORTED_CODES)}."
        )
    return base, has_sl


def _nz_inputs(ctx: PayrollContext) -> dict[str, Any]:
    """The engine's per-employee statutory inputs — ``ctx.extra['nz']``
    (direct callers/tests) overriding ``employee.extra['nz']`` (the
    stored convention). Missing entirely = hard refusal."""
    if ctx.extra and isinstance(ctx.extra.get("nz"), dict):
        return ctx.extra["nz"]
    employee_extra = getattr(ctx.employee, "extra", None)
    if isinstance(employee_extra, dict) and isinstance(employee_extra.get("nz"), dict):
        return employee_extra["nz"]
    raise NZPayrollUnsupported(
        "NZ payroll requires the employee's NZ statutory details at "
        "employee.extra['nz'] (or PayrollContext.extra['nz']) — at "
        "minimum {'tax_code': 'M'}. The Employee model has no NZ "
        "columns yet (schema change deferred); refusing to guess a "
        "tax code."
    )


# --------------------------------------------------------------------- #
# Engine                                                                #
# --------------------------------------------------------------------- #


class NZPayrollEngine:
    """NZ PAYE + KiwiSaver + ESCT + student loan behind the neutral seam.

    Embedded dated tables — ``session`` is accepted per the
    ``PayrollEngine`` protocol but not used (no reference-DB reads in
    this phase; re-sourcing to ``withholding_tables`` with embedded
    fallback is the later phase, same as AU's SG-rate plan).
    """

    jurisdiction = "NZ"

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        _require_supported_date(ctx.effective_date)
        if ctx.period not in _PERIODS_PER_YEAR:
            raise NZPayrollUnsupported(
                f"NZ payroll supports WEEKLY / FORTNIGHTLY / MONTHLY pay "
                f"periods; got {ctx.period!r}."
            )
        if ctx.gross < 0:
            raise NZPayrollError("gross must be non-negative")

        inputs = _nz_inputs(ctx)
        raw_code = inputs.get("tax_code")
        if not raw_code or not isinstance(raw_code, str):
            raise NZPayrollUnsupported(
                "employee.extra['nz']['tax_code'] is missing — refusing "
                "to guess (M vs secondary vs SL changes the number)."
            )
        base_code, has_sl = _parse_tax_code(raw_code)

        factor = _PERIODS_PER_YEAR[ctx.period]
        gross = round_money(Decimal(str(ctx.gross)))
        annual_gross = gross * factor
        levy_band = _acc_levy_for(ctx.effective_date)

        # ------ PAYE (income tax + ACC earner's levy) -------------------
        if base_code in _SUPPORTED_PRIMARY_CODES:
            annual_tax = _annual_income_tax(annual_gross)
        else:
            annual_tax = annual_gross * _SECONDARY_RATES[base_code]
        annual_levy = min(annual_gross, levy_band.earnings_cap) * levy_band.rate
        paye = round_money((annual_tax + annual_levy) / factor)
        tax_part = round_money(annual_tax / factor)
        levy_part = round_money(annual_levy / factor)
        levy_pct = (levy_band.rate * 100).quantize(Decimal("0.01"))
        paye_note = (
            f"PAYE {raw_code.strip().upper()}: annualised ${annual_gross} "
            f"({ctx.period} x{factor}); income tax ${tax_part}/period + "
            f"ACC earner's levy {levy_pct}% (cap ${levy_band.earnings_cap}) "
            f"${levy_part}/period = ${paye}"
        )

        components: list[PayrollComponent] = [
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=paye,
                note=paye_note,
            )
        ]

        # ------ Student loan --------------------------------------------
        student_loan = Decimal("0.00")
        if has_sl:
            if base_code in _SUPPORTED_PRIMARY_CODES:
                period_threshold = _SL_ANNUAL_THRESHOLD / factor
                over = gross - period_threshold
                student_loan = round_money(max(Decimal("0"), over) * _SL_RATE)
                sl_note = (
                    f"Student loan 12% over pay-period threshold "
                    f"${round_money(period_threshold)} (annual "
                    f"${_SL_ANNUAL_THRESHOLD}) = ${student_loan}"
                )
            else:
                # Secondary SL: 12% of the whole secondary income — the
                # repayment threshold applies to primary income only.
                student_loan = round_money(gross * _SL_RATE)
                sl_note = (
                    f"Student loan 12% of secondary income (no threshold) "
                    f"= ${student_loan}"
                )
            if student_loan > 0:
                components.append(
                    PayrollComponent(
                        role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                        amount=student_loan,
                        note=sl_note,
                    )
                )

        # ------ KiwiSaver + ESCT ----------------------------------------
        ks_employee = Decimal("0.00")
        ks_employer = Decimal("0.00")
        if inputs.get("kiwisaver_member"):
            ks = kiwisaver_rates_for(ctx.effective_date)

            raw_ee = inputs.get("kiwisaver_employee_rate_percent")
            ee_rate = (
                Decimal(str(raw_ee)) if raw_ee is not None
                else ks.default_employee_percent
            )
            if ee_rate not in ks.allowed_employee_percent:
                allowed = ", ".join(
                    str(r) for r in sorted(ks.allowed_employee_percent)
                )
                raise NZPayrollUnsupported(
                    f"KiwiSaver employee rate {ee_rate}% is not in the "
                    f"allowed set for {ctx.effective_date.isoformat()} "
                    f"({allowed}%)."
                )

            raw_er = inputs.get("kiwisaver_employer_rate_percent")
            er_rate = (
                Decimal(str(raw_er)) if raw_er is not None
                else ks.employer_min_percent
            )
            # The employer may match a temporary 3% opt-down at 3%
            # (§5.4); otherwise the dated minimum binds.
            er_floor = min(ks.employer_min_percent, ee_rate)
            if er_rate < er_floor:
                raise NZPayrollUnsupported(
                    f"KiwiSaver employer rate {er_rate}% is below the "
                    f"statutory minimum {ks.employer_min_percent}% in force "
                    f"on {ctx.effective_date.isoformat()} (matching a "
                    f"temporary 3% employee opt-down is the only permitted "
                    f"reduction)."
                )

            # KiwiSaver applies to GROSS salary/wages (incl. overtime) —
            # deliberately ctx.gross, NOT the AU OTE base.
            ks_employee = round_money(gross * ee_rate / Decimal("100"))
            ks_employer = round_money(gross * er_rate / Decimal("100"))

            raw_esct = inputs.get("esct_rate_percent")
            if raw_esct is not None:
                esct_rate = Decimal(str(raw_esct)) / Decimal("100")
                esct_basis = "employer-determined rate"
            else:
                # Estimate basis (permitted for employees without a full
                # previous year): annualised salary/wages + annualised
                # gross employer contributions.
                annual_esct_income = annual_gross + ks_employer * factor
                esct_rate = esct_rate_for(annual_esct_income)
                esct_basis = (
                    f"band estimate on annualised ${annual_esct_income}"
                )
            esct = round_money(ks_employer * esct_rate)
            esct_pct = (esct_rate * 100).quantize(Decimal("0.01"))

            if ks_employee > 0:
                components.append(
                    PayrollComponent(
                        role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                        amount=ks_employee,
                        note=(
                            f"KiwiSaver employee deduction {ee_rate}% of "
                            f"gross ${gross} = ${ks_employee}"
                        ),
                    )
                )
            ks_er_note = (
                f"KiwiSaver employer {er_rate}% of gross ${gross} = "
                f"${ks_employer} GROSS of ESCT; ESCT {esct_pct}% "
                f"({esct_basis}) = ${esct}, net to fund "
                f"${round_money(ks_employer - esct)}. Booked gross — the "
                f"ESCT/fund split is applied at remittance (payday filing)."
            )
            components.append(
                PayrollComponent(
                    role=PayrollComponentRole.RETIREMENT_LIABILITY,
                    amount=ks_employer,
                    note=ks_er_note,
                )
            )
            components.append(
                PayrollComponent(
                    role=PayrollComponentRole.RETIREMENT_EXPENSE,
                    amount=ks_employer,
                    note=ks_er_note,
                )
            )

        net = round_money(
            gross - paye - student_loan - ks_employee - ctx.deductions_total
        )
        return PayrollResult(
            jurisdiction="NZ",
            gross=gross,
            net=net,
            components=tuple(components),
        )


__all__ = [
    "NZPayrollEngine",
    "NZPayrollError",
    "NZPayrollUnsupported",
    "esct_rate_for",
    "kiwisaver_rates_for",
]
