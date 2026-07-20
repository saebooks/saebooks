"""UK payroll compute — cumulative PAYE, Class 1 NI, student loans,
auto-enrolment pension — behind the neutral ``PayrollEngine`` seam.

UK jurisdiction module. Tax year 2026-27 values; every figure is a
lock-step snapshot of this module's seed files
(``seeds/jurisdictions/UK/withholding_tables.yaml``,
``ni_thresholds.yaml``, ``pension_auto_enrolment.yaml``) whose primary
pulls are documented there. PAYE band tables are REFERENCE-PREFERRED
with this embedded fallback (the ``payroll_ee._resolve_rates``
convention — ``REFERENCE_DATABASE_URL`` is unset in the standard test/
CI harness, so the embedded path is what tests exercise); the NI /
student-loan / auto-enrolment parameters are embedded snapshots of
their ``reference_seed: false`` module-data seeds.

UK statutory inputs ride in ``PayrollContext.extra["uk"]``
----------------------------------------------------------
The neutral ``PayrollContext`` (and the ``Employee`` model) carry no UK
statutory fields — no HMRC tax code, NI category letter, YTD PAYE
figures or student-loan plan (adding employee columns is a schema
change, out of this module's scope — flagged in the build report). The
engine therefore reads a ``ctx.extra["uk"]`` mapping::

    {
        "tax_code": "1257L",          # required; S/C prefix selects nation
        "period_number": 3,            # required for cumulative codes (1-based)
        "ytd_taxable_pay": "6000.00",  # pay to date EXCLUDING this period
        "ytd_tax_paid": "590.40",      # tax to date EXCLUDING this period
        "ni_category": "A",            # default "A"
        "student_loan_plan": "PLAN_2", # optional: PLAN_1/PLAN_2/PLAN_4/PLAN_5
        "postgraduate_loan": True,     # optional; stacks with a plan
        "pension": {                   # optional auto-enrolment scheme
            "employee_percent": 5,
            "employer_percent": 3,
            "arrangement": "relief_at_source",
        },
    }

Hard refusals — never a silent wrong number (EE partial-month
precedent): a missing ``extra["uk"]`` block, K codes, Scottish/Welsh
FLAT codes (SBR/SD0/CBR/... — band mapping unverified), week 53/54/56
periods, payrolled benefits in kind, director (annual-basis) NI, NI
categories other than A/C, net-pay-arrangement pension schemes,
statutory-payment runs and cumulative refunds all raise
:class:`UKPayrollUnsupported` with a clear message.

What IS supported this wave: numeric tax codes with L/M/N/T suffixes
(cumulative and W1/M1/X non-cumulative) across all three nations
(no-prefix rUK, S Scotland six-band, C Wales), the flat rUK codes
BR/0T/D0/D1/NT, per-period Class 1 NI categories A and C, student-loan
plans 1/2/4/5 + postgraduate, and relief-at-source auto-enrolment
deductions on the qualifying-earnings band.

Approximations, stated: free pay uses the ``(code number x 10 + 9)``
annual-allowance convention apportioned by ``period/periods`` rather
than HMRC's Tables A pence-rounding; PAYE is truncated to whole pounds
of taxable pay and rounded down to the penny. Within HMRC tolerance,
but not byte-identical to the official test data — pinning against
HMRC's PAYE test vectors is follow-up work for RTI recognition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from saebooks.money import round_money
from saebooks.services.payroll.types import (
    PayrollComponent,
    PayrollComponentRole,
    PayrollContext,
    PayrollResult,
)


class UKPayrollError(Exception):
    """UK payroll compute failed."""


class UKPayrollUnsupported(UKPayrollError):
    """The input needs a UK payroll feature this wave deliberately does
    not implement — refuse loudly rather than emit a wrong number."""


_ZERO = Decimal("0")
_PENNY = Decimal("0.01")
_POUND = Decimal("1")

# Pay periods per year, keyed on the pay_frequency vocabulary
# ``pay_runs_v2`` passes through ``PayrollContext.period``.
_PERIODS_PER_YEAR: dict[str, int] = {
    "WEEKLY": 52,
    "FORTNIGHTLY": 26,
    "FOUR_WEEKLY": 13,
    "MONTHLY": 12,
}

# ---------------------------------------------------------------------------
# Embedded 2026-27 parameter snapshot (lock-step with the UK seeds).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PayeBand:
    lower: Decimal            # annual taxable income above the allowance
    upper: Decimal | None
    rate_percent: Decimal


@dataclass(frozen=True, slots=True)
class PayeTable:
    nation: str
    bands: tuple[PayeBand, ...]
    source: str = "embedded_fallback"


def _bands(*rows: tuple[str, str | None, str]) -> tuple[PayeBand, ...]:
    return tuple(
        PayeBand(
            lower=Decimal(lo),
            upper=None if hi is None else Decimal(hi),
            rate_percent=Decimal(rate),
        )
        for lo, hi, rate in rows
    )


_FALLBACK_PAYE_TABLES: dict[str, PayeTable] = {
    "england_ni": PayeTable(
        "england_ni",
        _bands(("0", "37700", "20"), ("37700", "125140", "40"), ("125140", None, "45")),
    ),
    "scotland": PayeTable(
        "scotland",
        _bands(
            ("0", "3967", "19"), ("3967", "16956", "20"),
            ("16956", "31092", "21"), ("31092", "62430", "42"),
            ("62430", "125140", "45"), ("125140", None, "48"),
        ),
    ),
    "wales": PayeTable(
        "wales",
        _bands(("0", "37700", "20"), ("37700", "125140", "40"), ("125140", None, "45")),
    ),
}

_PAYE_TABLE_CODES = {
    "england_ni": "uk_paye_ruk",
    "scotland": "uk_paye_scotland",
    "wales": "uk_paye_wales",
}

# Class 1 NI, category A (ni_thresholds.yaml snapshot). Weekly and
# monthly threshold values are exact HMRC figures; fortnightly/
# four-weekly use weekly x 2 / x 4 (the HMRC exact-percentage method).
_NI_WEEKLY = {"PT": Decimal("242"), "ST": Decimal("96"), "UEL": Decimal("967")}
_NI_MONTHLY = {"PT": Decimal("1048"), "ST": Decimal("417"), "UEL": Decimal("4189")}
_NI_EMPLOYEE_MAIN_PERCENT = Decimal("8")
_NI_EMPLOYEE_ADDITIONAL_PERCENT = Decimal("2")
_NI_EMPLOYER_PERCENT = Decimal("15")

EMPLOYMENT_ALLOWANCE_ANNUAL = Decimal("10500.00")

# Student loans (withholding_tables.yaml snapshot): plan -> (annual
# threshold, rate %). Deductions round DOWN to the whole pound.
_STUDENT_LOAN_PLANS: dict[str, tuple[Decimal, Decimal]] = {
    "PLAN_1": (Decimal("26900"), Decimal("9")),
    "PLAN_2": (Decimal("29385"), Decimal("9")),
    "PLAN_4": (Decimal("33795"), Decimal("9")),
    "PLAN_5": (Decimal("25000"), Decimal("9")),
}
_POSTGRAD_LOAN = (Decimal("21000"), Decimal("6"))

# Auto-enrolment qualifying-earnings band (pension_auto_enrolment.yaml).
_AE_BAND_LOWER_ANNUAL = Decimal("6240")
_AE_BAND_UPPER_ANNUAL = Decimal("50270")

_TAX_CODE_RE = re.compile(r"^(S|C)?(\d{1,4})(L|M|N|T)$")
_NON_CUMULATIVE_SUFFIX_RE = re.compile(r"\s*(W1/M1|W1|M1|X)$", re.IGNORECASE)
_FLAT_CODES_RUK = {"BR": Decimal("20"), "D0": Decimal("40"), "D1": Decimal("45")}


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_PENNY, rounding=ROUND_HALF_UP)


def _floor_penny(value: Decimal) -> Decimal:
    return value.quantize(_PENNY, rounding=ROUND_DOWN)


def _floor_pound(value: Decimal) -> Decimal:
    return value.quantize(_POUND, rounding=ROUND_DOWN)


async def _resolve_paye_table(session: Any, nation: str, effective_date: Any) -> PayeTable:
    """Reference-preferred / embedded-fallback PAYE band lookup (the
    ``payroll_ee._resolve_rates`` convention). Any absence — reference
    DB unconfigured, row missing, parameters malformed — degrades to
    the embedded 2026-27 snapshot; a real DB error is not swallowed
    beyond the documented absence case."""
    from saebooks.db import ReferenceSession

    if ReferenceSession is not None:
        from sqlalchemy import select

        from saebooks.models.reference.withholding_table import WithholdingTable

        async with ReferenceSession() as ref:
            row = (
                await ref.execute(
                    select(WithholdingTable)
                    .where(
                        WithholdingTable.jurisdiction == "GBR",
                        WithholdingTable.code == _PAYE_TABLE_CODES[nation],
                        WithholdingTable.effective_from <= effective_date,
                    )
                    .order_by(WithholdingTable.effective_from.desc())
                )
            ).scalars().first()
        if row is not None:
            params = row.parameters or {}
            brackets = params.get("brackets") or []
            if brackets:
                try:
                    bands = tuple(
                        PayeBand(
                            lower=Decimal(str(b["lower"])),
                            upper=(
                                None if b.get("upper") is None
                                else Decimal(str(b["upper"]))
                            ),
                            rate_percent=Decimal(str(b["rate_percent"])),
                        )
                        for b in brackets
                    )
                    return PayeTable(nation, bands, source="reference_db")
                except (KeyError, ArithmeticError, TypeError):
                    pass  # malformed row -> embedded snapshot below
    return _FALLBACK_PAYE_TABLES[nation]


@dataclass(frozen=True, slots=True)
class _ParsedTaxCode:
    nation: str                  # england_ni | scotland | wales
    allowance_annual: Decimal | None  # None => flat-rate code
    flat_rate_percent: Decimal | None
    no_tax: bool
    non_cumulative: bool
    raw: str


def _parse_tax_code(raw_code: str) -> _ParsedTaxCode:
    code = (raw_code or "").strip().upper()
    if not code:
        raise UKPayrollUnsupported(
            "no HMRC tax code supplied — set extra['uk']['tax_code'] "
            "(e.g. '1257L'); UK PAYE cannot be computed without one."
        )
    non_cumulative = False
    m = _NON_CUMULATIVE_SUFFIX_RE.search(code)
    if m:
        non_cumulative = True
        code = code[: m.start()].strip()

    if code.startswith("K") or code[:1] in ("S", "C") and code[1:2] == "K":
        raise UKPayrollUnsupported(
            f"tax code {raw_code!r} is a K code (negative allowance / "
            "untaxed-benefit clawback, with the 50% overriding limit) — "
            "not implemented this wave; refusing rather than "
            "under-collecting."
        )
    if code == "NT":
        return _ParsedTaxCode("england_ni", None, None, True, non_cumulative, raw_code)
    if code in _FLAT_CODES_RUK:
        return _ParsedTaxCode(
            "england_ni", None, _FLAT_CODES_RUK[code], False, non_cumulative, raw_code
        )
    if code[:1] in ("S", "C") and code[1:] in ("BR", "D0", "D1", "D2", "D3"):
        raise UKPayrollUnsupported(
            f"tax code {raw_code!r} is a Scottish/Welsh flat-rate code — "
            "the band each S/C flat code maps to was not primary-verified; "
            "refusing rather than guessing (use the numeric S/C code HMRC "
            "issued, e.g. 'S1257L')."
        )
    if code == "0T" or code[:1] in ("S", "C") and code[1:] == "0T":
        nation = {"S": "scotland", "C": "wales"}.get(code[:1], "england_ni")
        return _ParsedTaxCode(nation, _ZERO, None, False, non_cumulative, raw_code)

    m = _TAX_CODE_RE.match(code)
    if not m:
        raise UKPayrollUnsupported(
            f"tax code {raw_code!r} is not a supported shape — this wave "
            "implements numeric codes with L/M/N/T suffixes (optionally "
            "S/C-prefixed, optionally W1/M1/X), plus BR/0T/D0/D1/NT."
        )
    prefix, number, _suffix = m.groups()
    nation = {"S": "scotland", "C": "wales"}.get(prefix or "", "england_ni")
    # The (N x 10 + 9) annual-allowance convention (1257L -> 12,579).
    allowance = Decimal(number) * 10 + 9
    return _ParsedTaxCode(nation, allowance, None, False, non_cumulative, raw_code)


def _band_tax(bands: tuple[PayeBand, ...], taxable_annual_scale: Decimal) -> Decimal:
    """Tax on a taxable amount expressed on the bands' own (annual)
    scale — callers pre-scale the bands or the amount by period/periods."""
    tax = _ZERO
    for band in bands:
        if taxable_annual_scale <= band.lower:
            break
        upper = band.upper if band.upper is not None else taxable_annual_scale
        slice_amount = min(taxable_annual_scale, upper) - band.lower
        if slice_amount > 0:
            tax += slice_amount * band.rate_percent / Decimal("100")
    return tax


def employment_allowance_eligible(
    *, sole_employee_above_st_is_director: bool, public_body: bool = False
) -> bool:
    """The Employment Allowance exclusion rule (ni_thresholds.yaml): a
    company whose ONLY employee paid above the Secondary Threshold is
    also a director is not eligible; nor are public bodies. The
    allowance itself (GBP 10,500) offsets the employer's TOTAL Class 1
    secondary bill at remittance level — company-level, deliberately
    not part of per-employee ``compute_line`` output."""
    return not (sole_employee_above_st_is_director or public_body)


class UKPayrollEngine:
    """Cumulative PAYE + Class 1 NI + student loans + auto-enrolment
    behind the neutral seam."""

    jurisdiction = "UK"

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        uk = (ctx.extra or {}).get("uk")
        if not isinstance(uk, dict):
            raise UKPayrollUnsupported(
                "UK payroll needs its statutory inputs in "
                "extra['uk'] (tax_code, period_number, ytd figures, "
                "ni_category, ...) — the neutral PayrollContext and the "
                "Employee model carry no UK fields. Refusing rather than "
                "assuming an emergency code and category A."
            )
        periods = _PERIODS_PER_YEAR.get(ctx.period)
        if periods is None:
            raise UKPayrollUnsupported(
                f"pay frequency {ctx.period!r} is not supported for UK "
                f"payroll — supported: {sorted(_PERIODS_PER_YEAR)}."
            )
        if uk.get("payrolled_benefits"):
            raise UKPayrollUnsupported(
                "payrolling of benefits in kind is not implemented this "
                "wave (mandation phases from 6 April 2027) — process the "
                "benefit via P11D/Class 1A instead."
            )
        if uk.get("director"):
            raise UKPayrollUnsupported(
                "director NI uses an annual/pro-rata cumulative earnings "
                "period — not implemented this wave."
            )
        if uk.get("statutory_payment"):
            raise UKPayrollUnsupported(
                "statutory payments (SSP/SMP/SPP/...) are not computed "
                "this wave — rates are seeded in statutory_payments.yaml "
                "but the AWE/eligibility engine is not built."
            )

        gross = ctx.gross
        parsed = _parse_tax_code(str(uk.get("tax_code", "")))

        period_number = uk.get("period_number")
        if period_number is None:
            raise UKPayrollUnsupported(
                "extra['uk']['period_number'] (1-based position of this "
                "pay period in the tax year) is required — cumulative "
                "PAYE is undefined without it."
            )
        period_number = int(period_number)
        if period_number < 1:
            raise UKPayrollError("period_number must be >= 1")
        if period_number > periods:
            raise UKPayrollUnsupported(
                f"period {period_number} of {periods} is a week "
                "53/54/56-type extra period — its special non-cumulative "
                "free-pay treatment is not implemented this wave; refusing "
                "rather than over- or under-collecting."
            )

        paye = await self._compute_paye(
            session, parsed, gross, uk, period_number, periods, ctx
        )

        ni_category = str(uk.get("ni_category", "A")).upper()
        employee_ni, employer_ni = self._compute_ni(gross, ni_category, ctx.period)

        sl_components = self._compute_student_loans(gross, uk, periods)

        pension_ee, pension_er = self._compute_pension(gross, uk, periods)

        deductions = ctx.deductions_total
        sl_total = sum((amt for _, amt in sl_components), start=_ZERO)
        net = round_money(
            gross - paye - employee_ni - sl_total - pension_ee - deductions
        )

        components: list[PayrollComponent] = [
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=paye,
                note=(
                    f"PAYE income tax, code {parsed.raw} "
                    f"({parsed.nation}), period {period_number}/{periods}"
                ),
            ),
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=employee_ni,
                note=f"Class 1 NI (employee), category {ni_category}",
            ),
        ]
        for label, amount in sl_components:
            components.append(
                PayrollComponent(
                    role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                    amount=amount,
                    note=label,
                )
            )
        components.append(
            PayrollComponent(
                role=PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY,
                amount=employer_ni,
                note=f"Class 1 NI (employer, secondary), category {ni_category}",
            )
        )
        components.append(
            PayrollComponent(
                role=PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE,
                amount=employer_ni,
                note=f"Class 1 NI (employer, secondary), category {ni_category}",
            )
        )
        if pension_ee or pension_er:
            components.append(
                PayrollComponent(
                    role=PayrollComponentRole.RETIREMENT_LIABILITY,
                    amount=_q2(pension_ee + pension_er),
                    note=(
                        "Auto-enrolment pension payable (employee "
                        f"{pension_ee} + employer {pension_er}, relief at "
                        "source, qualifying-earnings band)"
                    ),
                )
            )
            components.append(
                PayrollComponent(
                    role=PayrollComponentRole.RETIREMENT_EXPENSE,
                    amount=pension_er,
                    note="Auto-enrolment pension — employer contribution",
                )
            )

        return PayrollResult(
            jurisdiction="UK",
            gross=gross,
            net=net,
            components=tuple(components),
        )

    # -- PAYE ---------------------------------------------------------------

    async def _compute_paye(
        self,
        session: Any,
        parsed: _ParsedTaxCode,
        gross: Decimal,
        uk: dict[str, Any],
        period_number: int,
        periods: int,
        ctx: PayrollContext,
    ) -> Decimal:
        if parsed.no_tax:
            return _ZERO
        if parsed.flat_rate_percent is not None:
            return _floor_penny(gross * parsed.flat_rate_percent / Decimal("100"))

        table = await _resolve_paye_table(session, parsed.nation, ctx.effective_date)
        allowance = parsed.allowance_annual or _ZERO

        if parsed.non_cumulative:
            # W1/M1/X — each period stands alone on 1/periods of the
            # allowance and bands.
            free_pay = allowance / periods
            taxable = _floor_pound(max(_ZERO, gross - free_pay))
            scaled = Decimal(periods)
            tax = _band_tax(
                tuple(
                    PayeBand(
                        b.lower / scaled,
                        None if b.upper is None else b.upper / scaled,
                        b.rate_percent,
                    )
                    for b in table.bands
                ),
                taxable,
            )
            return _floor_penny(tax)

        ytd_pay = Decimal(str(uk.get("ytd_taxable_pay", "0")))
        ytd_tax = Decimal(str(uk.get("ytd_tax_paid", "0")))
        fraction = Decimal(period_number) / Decimal(periods)

        pay_to_date = ytd_pay + gross
        free_pay_to_date = _q2(allowance * fraction)
        taxable_to_date = _floor_pound(max(_ZERO, pay_to_date - free_pay_to_date))
        scaled_bands = tuple(
            PayeBand(
                _q2(b.lower * fraction),
                None if b.upper is None else _q2(b.upper * fraction),
                b.rate_percent,
            )
            for b in table.bands
        )
        tax_to_date = _floor_penny(_band_tax(scaled_bands, taxable_to_date))
        tax_this_period = tax_to_date - ytd_tax
        if tax_this_period < 0:
            raise UKPayrollUnsupported(
                "cumulative PAYE computes a refund for this period "
                f"({tax_this_period}) — in-payroll refunds are not "
                "implemented this wave (the neutral posting profile has "
                "no negative-withholding path); refusing rather than "
                "clamping to zero and over-collecting."
            )
        return _q2(tax_this_period)

    # -- National Insurance --------------------------------------------------

    def _compute_ni(
        self, gross: Decimal, category: str, period: str
    ) -> tuple[Decimal, Decimal]:
        if category not in ("A", "C"):
            raise UKPayrollUnsupported(
                f"NI category {category!r} is not computed this wave — "
                "only A (standard) and C (over State Pension age) carry "
                "primary-verified rates; the relief/reduced categories "
                "(B/H/J/M/V/Z/Freeport) are seeded structurally in "
                "ni_thresholds.yaml with rates_status UNVERIFIED."
            )
        if period == "MONTHLY":
            pt, st, uel = _NI_MONTHLY["PT"], _NI_MONTHLY["ST"], _NI_MONTHLY["UEL"]
        else:
            mult = {"WEEKLY": 1, "FORTNIGHTLY": 2, "FOUR_WEEKLY": 4}[period]
            pt = _NI_WEEKLY["PT"] * mult
            st = _NI_WEEKLY["ST"] * mult
            uel = _NI_WEEKLY["UEL"] * mult

        if category == "C":
            employee = _ZERO
        else:
            main = max(_ZERO, min(gross, uel) - pt)
            additional = max(_ZERO, gross - uel)
            employee = _q2(
                main * _NI_EMPLOYEE_MAIN_PERCENT / Decimal("100")
                + additional * _NI_EMPLOYEE_ADDITIONAL_PERCENT / Decimal("100")
            )
        employer = _q2(
            max(_ZERO, gross - st) * _NI_EMPLOYER_PERCENT / Decimal("100")
        )
        return employee, employer

    # -- Student loans ---------------------------------------------------------

    def _compute_student_loans(
        self, gross: Decimal, uk: dict[str, Any], periods: int
    ) -> list[tuple[str, Decimal]]:
        out: list[tuple[str, Decimal]] = []
        plan = uk.get("student_loan_plan")
        if plan:
            plan = str(plan).upper()
            if plan not in _STUDENT_LOAN_PLANS:
                raise UKPayrollUnsupported(
                    f"student loan plan {plan!r} unknown — supported: "
                    f"{sorted(_STUDENT_LOAN_PLANS)} (+ postgraduate_loan)."
                )
            threshold_annual, rate = _STUDENT_LOAN_PLANS[plan]
            amount = self._loan_deduction(gross, threshold_annual, rate, periods)
            if amount:
                out.append((f"Student loan {plan} (9%-family, floor-pound)", amount))
        if uk.get("postgraduate_loan"):
            threshold_annual, rate = _POSTGRAD_LOAN
            amount = self._loan_deduction(gross, threshold_annual, rate, periods)
            if amount:
                out.append(("Postgraduate loan (6%, floor-pound)", amount))
        return out

    @staticmethod
    def _loan_deduction(
        gross: Decimal, threshold_annual: Decimal, rate: Decimal, periods: int
    ) -> Decimal:
        threshold = _q2(threshold_annual / periods)
        excess = max(_ZERO, gross - threshold)
        return _floor_pound(excess * rate / Decimal("100"))

    # -- Auto-enrolment pension --------------------------------------------------

    def _compute_pension(
        self, gross: Decimal, uk: dict[str, Any], periods: int
    ) -> tuple[Decimal, Decimal]:
        pension = uk.get("pension")
        if not pension:
            return _ZERO, _ZERO
        arrangement = str(pension.get("arrangement", "relief_at_source"))
        if arrangement != "relief_at_source":
            raise UKPayrollUnsupported(
                f"pension arrangement {arrangement!r} is not implemented — "
                "net-pay-arrangement schemes change the PAYE taxable base; "
                "only relief_at_source is supported this wave."
            )
        lower = _q2(_AE_BAND_LOWER_ANNUAL / periods)
        upper = _q2(_AE_BAND_UPPER_ANNUAL / periods)
        qualifying = max(_ZERO, min(gross, upper) - lower)
        ee = _q2(
            qualifying * Decimal(str(pension.get("employee_percent", 0))) / Decimal("100")
        )
        er = _q2(
            qualifying * Decimal(str(pension.get("employer_percent", 0))) / Decimal("100")
        )
        return ee, er
