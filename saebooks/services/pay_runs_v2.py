"""Pay-run orchestration with PAYG + Super calc + per-employee JE.

This is the Phase 2 successor to ``services/pay_runs.py``. Where the
v1 service treated each ``pay_run_line`` as a hand-entered
``(gross, tax, super, net)`` quadruple and produced one lump-sum JE
per run, the v2 service:

* Takes the **inputs** to a pay-line (ordinary hours, overtime hours,
  allowances, deductions, paid leave, lump sums, OTE) and computes
  the withholding + retirement amounts via the jurisdiction payroll
  engine (``services.payroll.get_payroll_engine``; AU's PAYG/super
  compute lives in ``saebooks.jurisdictions.au``).
* Writes the full extended ``pay_run_lines`` row (Phase 1B shape:
  ordinary_hours, overtime_hours, allowances jsonb, deductions jsonb,
  paid_leave jsonb, lump_sums jsonb, ytd_gross, ytd_tax, ytd_super).
* Is **idempotent** per ``(pay_run_id, employee_id)``: a second call
  replaces the existing line with the new computation (so the
  operator can edit hours, hit "Recalculate", and see the update
  without delete-then-recreate).
* On finalize, generates a **per-employee** JE:
    Dr Wages expense       gross
    Dr Super expense       sg
       Cr PAYG WH liability      payg
       Cr Super payable          sg
       Cr Bank / wages payable   net
* Updates YTD running totals so the next pay-line for the same
  employee can read them.

The Phase 1B-extended ``pay_run_lines`` columns (ordinary_hours,
overtime_hours, allowances JSONB, deductions JSONB, paid_leave JSONB,
lump_sums JSONB, ytd_gross, ytd_tax, ytd_super) are assumed present —
this module does NOT touch the schema; that's Phase 1B's job. Read
``pay_run_lines`` columns via raw column expressions (``c.<name>``)
rather than ORM attributes so the model class doesn't need to be
re-declared here. If/when Phase 1B updates the ORM class, this
module migrates to attribute access transparently.

Account resolution (per Chart of Accounts seed):

    Wages expense:        6-XXXX (kind = EXPENSE) — first match by code prefix.
                          Convention: 6-2110 "Wages — gross".
    Super expense:        6-XXXX — 6-2120 "Superannuation expense".
    PAYG WH liability:    2-1310 "PAYG withholding payable".
    Super payable:        2-1320 "Superannuation payable".
    Net pay clearing:     2-1150 "Payments — pending" (existing).

Missing accounts raise ``PayRunError`` — operator must seed before
running payroll. The codes mirror the CoA seed for ``books.primary``.
The brief calls for "Cr Bank" but in practice the bank credit doesn't
happen until reconciliation — the v1 service already credits
``2-1150 Payments - Pending`` (cleared by ABA processing), so we
keep that pattern.

Out of scope for this pass:
    - STP submission (Phase 3)
    - ABA-file generation (re-use v1 ``export_aba`` after this
      finalises — same shape on the wire)
    - Leave accrual write-back (Phase 3)

EE finalize (Packet 1, kmd-inf-tsd follow-up) — ``_finalize_ee``, wired
through ``finalize_with_je`` for ``Company.jurisdiction == "EE"``:

* On finalize, generates a per-employee JE:
    Dr Wages expense                 gross
    Dr Social tax expense            social_tax (33%, incl. EUR 886 floor)
    Dr Employer unemployment expense unemployment_employer (0.8%)
       Cr Income tax payable             income_tax
       Cr Unemployment payable (employee)  unemployment_employee (1.6%)
       Cr Pillar II payable              pillar_ii
       Cr Social tax payable             social_tax
       Cr Employer unemployment payable  unemployment_employer
       Cr Net wages payable              net
* Account resolution is per-company-column-driven
  (``_account_by_company_column``), NOT fixed chart codes like the AU
  posting profile's — EE has no chart-template seed in this tree to
  hardcode against. See the ``_EE_*_COLUMN`` constants below for the
  9 keys.
* Reversal: ``void_pay_run`` (generic, not EE-specific — reverses
  whichever journal a FINALIZED pay run posted, via ``journal_svc.reverse``).
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, extract, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.employee import Employee, PayFrequency
from saebooks.models.journal import EntryStatus, JournalEntry, JournalOrigin
from saebooks.models.pay_run import PayRun, PayRunLine, PayRunStatus
from saebooks.money import round_money
from saebooks.services import change_log as cl_svc
from saebooks.services import journal as journal_svc
from saebooks.services.fringe_benefits_ee import (
    CarBenefitRates,
    EEFringeBenefitResult,
    compute_car_fringe_benefit,
    compute_cash_fringe_benefit,
    resolve_car_benefit_rates,
)
from saebooks.services.payroll import (
    PayrollComponentRole,
    PayrollContext,
    PayrollRoleAccount,
    get_payroll_engine,
    get_posting_profile,
)
from saebooks.services.payroll_ee import (
    EEPayrollResult,
    compute_ee_payroll,
    resolve_ee_rates,
)


class PayRunV2Error(ValueError):
    """Domain-level failure during a v2 pay-run operation."""


# Account-code conventions for the finalize JE live in each
# jurisdiction's payroll posting profile (jurisdiction-module Phase 1):
# AU's fixed chart codes are ``jurisdictions.au.PAYROLL_POSTING`` (the
# former ``_ACCT_*`` constants here), the no-module floor is
# ``payroll.neutral.NEUTRAL_POSTING_PROFILE``. Override via company
# settings is still the Phase 2B plan.


# --------------------------------------------------------------------- #
# EE account resolution (Packet 1 — GL posting for EE pay-run finalize) #
# --------------------------------------------------------------------- #
#
# Unlike AU's posting profile (fixed chart codes, ``_account_by_code``),
# EE has no chart-template seed anywhere in this tree yet (no
# ``seed/load_ee_coa.py`` — checked). Rather than invent fixed EE codes
# that would silently collide with whatever a real EE company's chart
# actually uses, each of these resolves to a per-company override
# COLUMN on ``companies`` (0200, Fixer round 4 F1 — was a global
# ``Setting`` row until this fix; see ``_account_by_company_column``'s
# docstring for why that was wrong for a multi-company instance),
# raising loudly (never posting an unbalanced or wrongly-coded JE) when
# the column is unset or does not resolve. One column per journal leg —
# the packet enumerates the components (income tax / unemployment
# employee / pillar II / social tax / employer unemployment / net pay)
# separately, so this mirrors that granularity rather than collapsing
# them into fewer accounts. Values match the ``Company`` model
# attribute names 1:1.
_EE_WAGES_EXPENSE_COLUMN = "ee_payroll_wages_expense_account_code"
_EE_SOCIAL_TAX_EXPENSE_COLUMN = "ee_payroll_social_tax_expense_account_code"
_EE_UNEMPLOYMENT_EMPLOYER_EXPENSE_COLUMN = (
    "ee_payroll_unemployment_employer_expense_account_code"
)
_EE_INCOME_TAX_PAYABLE_COLUMN = "ee_payroll_income_tax_payable_account_code"
_EE_UNEMPLOYMENT_EMPLOYEE_PAYABLE_COLUMN = (
    "ee_payroll_unemployment_employee_payable_account_code"
)
_EE_PILLAR_II_PAYABLE_COLUMN = "ee_payroll_pillar_ii_payable_account_code"
_EE_SOCIAL_TAX_PAYABLE_COLUMN = "ee_payroll_social_tax_payable_account_code"
_EE_UNEMPLOYMENT_EMPLOYER_PAYABLE_COLUMN = (
    "ee_payroll_unemployment_employer_payable_account_code"
)
_EE_NET_PAY_CLEARING_COLUMN = "ee_payroll_net_pay_clearing_account_code"

# Fringe-benefit (erisoodustus) tax legs — Packet 2. Resolved LAZILY,
# only when a pay run actually has a nonzero fringe-benefit total (see
# ``_finalize_ee``): existing EE companies with no fringe-benefit
# columns configured must keep finalizing exactly as before this
# packet (mirrors the 9 columns above being resolved unconditionally
# because every EE pay run always has SOME wage withholding — a fringe
# benefit is optional, so these 4 must not become a hard finalize
# blocker for a company that never uses them).
_EE_FRINGE_BENEFIT_INCOME_TAX_EXPENSE_COLUMN = (
    "ee_payroll_fringe_benefit_income_tax_expense_account_code"
)
_EE_FRINGE_BENEFIT_SOCIAL_TAX_EXPENSE_COLUMN = (
    "ee_payroll_fringe_benefit_social_tax_expense_account_code"
)
_EE_FRINGE_BENEFIT_INCOME_TAX_PAYABLE_COLUMN = (
    "ee_payroll_fringe_benefit_income_tax_payable_account_code"
)
_EE_FRINGE_BENEFIT_SOCIAL_TAX_PAYABLE_COLUMN = (
    "ee_payroll_fringe_benefit_social_tax_payable_account_code"
)


# --------------------------------------------------------------------- #
# Inputs                                                                #
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class AllowanceLine:
    """One allowance — STP2 itemised."""

    type: str            # e.g. "CD" (cents-per-km), "MD" (meal), "TR" (travel)
    amount: Decimal      # dollars (2 dp)
    is_ote: bool = True  # if True, included in OTE for super
    is_taxable: bool = True   # if True, included in PAYG gross


@dataclasses.dataclass(frozen=True)
class DeductionLine:
    """One deduction taken from net pay (after tax)."""

    type: str            # STP2 deduction code, e.g. "F" (workplace giving)
    amount: Decimal


@dataclasses.dataclass(frozen=True)
class PaidLeaveLine:
    """Paid leave hours + dollars (treated as OTE for super)."""

    leave_type: str      # "ANNUAL" / "PERSONAL" / "LONG_SERVICE" / "OTHER"
    hours: Decimal
    amount: Decimal      # dollars
    is_ote: bool = True


@dataclasses.dataclass(frozen=True)
class LumpSumLine:
    """Lump sums (A/B/D/E/W in STP2 terms).

    A = back-pay for past-year service
    B = unused-leave at termination, pre-1983 portion
    D = redundancy / ETP tax-free
    E = back-pay > $1,200, taxed via marginal-rate apportionment
    W = lump-sum-in-arrears (current-year back-pay)

    For now we record them and include the taxable portion in gross;
    Phase 2B will apply the schedule-5 marginal-rate apportionment
    formula. Caller pre-computes the "treat as if for this period"
    component, or leaves the lump for later via flag.
    """

    category: str           # 'A' / 'B' / 'D' / 'E' / 'W'
    amount: Decimal
    is_taxable: bool = True
    is_ote: bool = False    # lump sums are not OTE per SGR 2009/2


@dataclasses.dataclass(frozen=True)
class FringeBenefitLine:
    """One EE fringe-benefit (erisoodustus) event for this pay-run line
    (kmd-inf-tsd follow-up, Packet 2). EE-only — ``_compute`` (the AU
    branch) never reads this field, mirrors ``medicare_exemption`` being
    EE-inert.

    Exactly ONE of two shapes, validated in ``__post_init__``:

    * Company car — ``engine_power_kw`` + ``car_age_years`` set,
      ``taxable_value`` left ``None``. Valued via
      ``services.fringe_benefits_ee.compute_car_fringe_benefit`` (EUR/kW
      /month, reduced past 5 years).
    * Generic cash-value benefit (housing, entertainment, ...) —
      ``taxable_value`` set, the car fields left ``None``. Valued via
      ``services.fringe_benefits_ee.compute_cash_fringe_benefit`` (the
      22/78 + 33% formula only, no valuation step — caller has already
      determined the value).

    Both shapes go through the SAME income-tax/social-tax formula; only
    how ``taxable_value`` is arrived at differs.
    """

    benefit_category: str  # e.g. "motor_vehicle", "housing", "other"
    engine_power_kw: Decimal | None = None
    car_age_years: int | None = None
    taxable_value: Decimal | None = None

    def __post_init__(self) -> None:
        car_shape = self.engine_power_kw is not None and self.car_age_years is not None
        cash_shape = self.taxable_value is not None
        if car_shape == cash_shape:  # both set, or neither
            raise PayRunV2Error(
                "FringeBenefitLine requires EITHER (engine_power_kw + "
                "car_age_years) for the company-car basis OR "
                "taxable_value for a generic cash-value benefit — not "
                "both, not neither."
            )


@dataclasses.dataclass(frozen=True)
class PayLineInput:
    """The complete set of inputs needed to compute one pay-line."""

    employee_id: uuid.UUID
    ordinary_hours: Decimal = Decimal("0")
    overtime_hours: Decimal = Decimal("0")
    ordinary_rate: Decimal | None = None   # override of Employee.base_rate
    overtime_multiplier: Decimal = Decimal("1.5")
    allowances: tuple[AllowanceLine, ...] = ()
    deductions: tuple[DeductionLine, ...] = ()
    paid_leave: tuple[PaidLeaveLine, ...] = ()
    lump_sums: tuple[LumpSumLine, ...] = ()
    # Opaque pass-through to the jurisdiction payroll engine (mirrors the
    # neutral ``PayrollContext.medicare_exemption: str`` seam field); core never
    # branches on it. Was the AU ``payg.MedicareExemption`` Literal — now plain
    # str so core carries no AU-shaped payroll type.
    medicare_exemption: str = "NONE"
    fringe_benefits: tuple[FringeBenefitLine, ...] = ()
    # Phase 2B will surface these via ``employee.extra`` JSONB; for
    # now caller passes explicitly.

    def __post_init__(self) -> None:
        for fld_name, fld_val in (
            ("ordinary_hours", self.ordinary_hours),
            ("overtime_hours", self.overtime_hours),
            ("overtime_multiplier", self.overtime_multiplier),
        ):
            if fld_val < 0:
                raise PayRunV2Error(f"{fld_name} must be non-negative")


@dataclasses.dataclass(frozen=True)
class ComputedPayLine:
    """The output of the calc — what gets written to ``pay_run_lines``."""

    employee_id: uuid.UUID
    gross: Decimal
    payg: Decimal
    super_amount: Decimal
    net: Decimal
    ordinary_hours: Decimal
    overtime_hours: Decimal
    allowances: list[dict[str, Any]]
    deductions: list[dict[str, Any]]
    paid_leave: list[dict[str, Any]]
    lump_sums: list[dict[str, Any]]
    ote: Decimal
    payg_breakdown: str
    super_breakdown: str
    # --- EE payroll compute (Packet 3) — None on every AU-jurisdiction
    # line; ``payg``/``super_amount`` above stay explicitly 0 (not
    # left holding a stale AU number) when this branch is populated,
    # to avoid the "wrong return filed" footgun (scope §1.2). ---
    ee_income_tax: Decimal | None = None
    ee_unemployment_employee: Decimal | None = None
    ee_unemployment_employer: Decimal | None = None
    ee_social_tax: Decimal | None = None
    ee_pillar_ii: Decimal | None = None
    # --- EE fringe-benefit compute (Packet 2) — [] / None on every AU
    # line AND every EE line with no fringe_benefits input (the common
    # case). A separate EE tax event from ordinary withholding above —
    # see services.fringe_benefits_ee module docstring. ---
    ee_fringe_benefits: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    ee_fringe_benefit_income_tax: Decimal | None = None
    ee_fringe_benefit_social_tax: Decimal | None = None


# --------------------------------------------------------------------- #
# Account lookups                                                       #
# --------------------------------------------------------------------- #


async def _account_by_code(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str,
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == code,
            Account.archived_at.is_(None),
        )
    )
    acct = result.scalars().first()
    if acct is None:
        raise PayRunV2Error(
            f"Account {code} not found for this company. "
            "Re-run the AU CoA seed or create the account manually."
        )
    return acct


async def _account_by_company_column(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    column_name: str,
) -> Account:
    """Resolve a statutory GL account for EE pay-run posting via a
    per-company override column on ``companies`` (0200).

    Fixer round 4 (F1): this used to read a GLOBAL ``Setting`` row
    (``settings.key`` is that table's sole primary key — no
    ``company_id`` column at all), mirroring
    ``services.bills._get_rc_payable_account``'s precedent. That was
    wrong for a multi-company instance: two EE companies could not
    configure this independently, and if the resolved code happened to
    already exist in a second company's own chart for an unrelated
    purpose, that company's payroll finalize would silently book to the
    WRONG account — no error, journal still balances. Same class of bug
    0198 (``ar_control_account_code`` / ``ap_control_account_code``)
    fixed for AR/AP; this brings EE payroll account resolution in line
    with that per-company pattern. Raises loudly — a config error must
    never silently produce an unbalanced or wrongly-coded journal (same
    posture as before)."""
    column = getattr(Company, column_name)
    raw = (
        await session.execute(select(column).where(Company.id == company_id))
    ).scalar_one_or_none()
    code = (str(raw) if raw is not None else "").strip()
    acct = None
    if code:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == code,
                    Account.archived_at.is_(None),
                )
            )
        ).scalars().first()
    if acct is None:
        raise PayRunV2Error(
            f"Cannot finalize this EE pay run: companies.{column_name} "
            f"({code!r}) does not resolve to an account in this company's "
            "chart. Set it on this company before finalizing — EE "
            "payroll has no default chart-of-accounts seed to fall back on."
        )
    return acct


# --------------------------------------------------------------------- #
# Calc                                                                  #
# --------------------------------------------------------------------- #


def _q(value: Decimal | int | float | str) -> Decimal:
    """Quantize a value to 2 dp (half-up)."""
    return round_money(Decimal(str(value)))


def _fringe_benefit_to_dict(result: EEFringeBenefitResult) -> dict[str, Any]:
    """``dataclasses.asdict`` leaves ``Decimal`` fields as ``Decimal`` —
    the stdlib ``json`` serializer SQLAlchemy's JSONB type uses by
    default cannot encode those, so this stringifies every Decimal
    field explicitly before the dict is written to the
    ``pay_run_lines.ee_fringe_benefits`` JSONB column."""
    d = dataclasses.asdict(result)
    for key, val in d.items():
        if isinstance(val, Decimal):
            d[key] = str(val)
    return d


def _resolve_ordinary_rate(emp: Employee, override: Decimal | None) -> Decimal:
    if override is not None:
        return Decimal(str(override))
    # PayBasis = SALARY: rate is per-annum, convert to per-hour.
    if emp.pay_basis == "SALARY":
        weekly = Decimal(str(emp.base_rate)) / Decimal("52")
        per_hour = weekly / Decimal(str(emp.weekly_hours))
        return per_hour
    return Decimal(str(emp.base_rate))


async def _ee_same_month_conflict(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    company_id: uuid.UUID,
    period_start: date,
    exclude_pay_run_id: uuid.UUID,
) -> bool:
    """True if this employee already has a pay-run line in ANOTHER pay
    run (any status, not archived) whose own period falls in the same
    calendar month as ``period_start`` — critic round 4 finding, see
    ``_compute_ee``'s "same-month floor" guard. Mirrors
    ``_ytd_for_employee``'s join shape (``PayRunLine`` -> ``PayRun``,
    scoped by ``company_id``/``employee_id``, excluding this pay run,
    excluding archived runs)."""
    stmt = (
        select(func.count())
        .select_from(PayRunLine)
        .join(PayRun, PayRun.id == PayRunLine.pay_run_id)
        .where(
            PayRun.company_id == company_id,
            PayRunLine.employee_id == employee_id,
            PayRun.archived_at.is_(None),
            PayRun.id != exclude_pay_run_id,
            extract("year", PayRun.period_start) == period_start.year,
            extract("month", PayRun.period_start) == period_start.month,
        )
    )
    result = await session.execute(stmt)
    return (result.scalar_one() or 0) > 0


async def _compute_ee(
    session: AsyncSession,
    *,
    employee: Employee,
    line_input: PayLineInput,
    effective_date: date,
    period_start: date,
    period_end: date,
    pay_run_id: uuid.UUID,
) -> ComputedPayLine:
    """EE payroll compute path (Packet 3) — parallel to ``_compute``'s
    AU path, dispatched by ``Company.jurisdiction`` in ``upsert_line``.
    Ordinary gross-wage payments only (scope §2.2's board-member-fee /
    allowance flag — not modelled here). Pure (no writes); rate lookup
    happens inside ``services.payroll_ee.compute_ee_payroll``.

    ⚠ **MONTHLY-only guard (critic round 2 finding).** Unlike the AU
    branch two lines below (which reads ``employee.pay_frequency`` and
    scales via ``jurisdictions.au.payg``'s period tables), this function never
    read ``pay_frequency`` at all — the EUR 700/776 basic exemption and
    the EUR 886/mo social-tax floor
    (``services.payroll_ee.compute_ee_payroll``) are ``[SEED-EE]``
    MONTHLY figures, applied in full to every call regardless of period
    length. Against the model's own default (``pay_frequency`` =
    WEEKLY, ``employee.py``), that silently overstated social tax up to
    ~4x for a weekly-paid EE employee. Refusing loudly for any
    non-MONTHLY frequency is the tax-safe fix: a wrong-but-plausible
    number filed with EMTA is worse than a blocked pay run.

    ⚠ **Same-month multi-pay-run guard (critic round 4 fix — closes the
    gap this docstring used to flag as unfixed).** Even at MONTHLY
    frequency, two pay runs for one employee inside the same calendar
    month would each get the EUR 886 floor / EUR 700-776 exemption
    applied independently (this compute is per-line, not
    per-employee-per-calendar-month), overstating the aggregate. TRUE
    monthly aggregation (computing tax on the combined gross and
    crediting what earlier pay runs already withheld) is a bigger,
    UNVERIFIED-ordering change — see the module docstring's own "No
    partial-period proration" flag for the same class of gap. Rather
    than build that, this refuses loudly the moment a SECOND pay-run
    line for the same employee lands in the same calendar month
    (``_ee_same_month_conflict``) — same tax-safe posture as the two
    guards below: a blocked pay run beats a silently doubled floor.

    ⚠ **Full-period-employment guard (critic round 3 finding).**
    ``payroll_ee.compute_ee_payroll`` takes no employment-start/-end
    input and applies the full EUR 700/776 exemption + EUR 886 floor
    regardless — correct for an employee who worked the WHOLE
    ``period_start..period_end`` span, wrong (unverified which
    direction) for a mid-period hire/termination. Rather than guess a
    proration rule (same UNVERIFIED-citation problem as the module
    docstring's existing flag), refuse loudly whenever
    ``employee.start_date``/``end_date`` falls inside the pay run's own
    period — same tax-safe posture as the MONTHLY-only guard above: a
    blocked pay run beats a wrong-but-plausible EMTA filing.
    """
    if employee.pay_frequency != PayFrequency.MONTHLY.value:
        raise PayRunV2Error(
            "EE payroll compute only supports pay_frequency=MONTHLY "
            f"today — employee {employee.id} is {employee.pay_frequency!r}. "
            "The EUR 700/776 basic exemption and EUR 886 social-tax "
            "floor (services.payroll_ee) are monthly figures with no "
            "period-scaling implemented; applying them per-line at a "
            "shorter period would misstate income tax and social tax "
            "(critic round 2 finding). Set the employee to MONTHLY, or "
            "wait for period-scaling to be built."
        )
    if employee.start_date > period_start or (
        employee.end_date is not None and employee.end_date < period_end
    ):
        raise PayRunV2Error(
            f"EE payroll compute refuses a partial-period line for "
            f"employee {employee.id}: employment span "
            f"({employee.start_date}..{employee.end_date or 'open'}) does "
            f"not fully cover the pay run's period "
            f"({period_start}..{period_end}). The EUR 700/776 exemption "
            "and EUR 886 social-tax floor are monthly figures applied in "
            "full with no mid-period proration implemented (correct "
            "proration is UNVERIFIED against Tulumaksuseadus/EMTA — "
            "critic round 3 finding); filing a wrong-but-plausible "
            "figure is worse than a blocked pay run. Adjust the pay "
            "run's period to match the employment span, or wait for "
            "proration to be built."
        )
    if await _ee_same_month_conflict(
        session, employee_id=employee.id, company_id=employee.company_id,
        period_start=period_start, exclude_pay_run_id=pay_run_id,
    ):
        raise PayRunV2Error(
            f"EE payroll compute refuses a second pay-run line for "
            f"employee {employee.id} within the same calendar month as "
            f"this pay run's period ({period_start:%Y-%m}) — critic round "
            "4 finding: the EUR 886 social-tax floor and EUR 700/776 "
            "exemption are per-CALENDAR-MONTH figures but this compute "
            "runs per pay-run-line, so a second pay run for the same "
            "employee in the same month would apply the floor/exemption "
            "independently and overstate the aggregate month (true "
            "cross-pay-run aggregation is a bigger, UNVERIFIED-ordering "
            "change — see this function's own docstring). Void or "
            "combine the other pay run's line for this employee into a "
            "single pay run for the month instead."
        )
    ordinary_rate = _resolve_ordinary_rate(employee, line_input.ordinary_rate)
    ordinary_pay = _q(line_input.ordinary_hours * ordinary_rate)
    ot_rate = _q(ordinary_rate * line_input.overtime_multiplier)
    overtime_pay = _q(line_input.overtime_hours * ot_rate)
    gross = _q(ordinary_pay + overtime_pay)

    # Fixer round 5 (F4): resolve EERates ONCE for this call and reuse it
    # for both the wage-withholding compute below AND every fringe-benefit
    # compute in the loop further down — same "resolve once, reuse"
    # convention ``compute_ee_payroll``'s own ``rates`` kwarg docstring
    # describes (and that ``services.lodgement.tsd.generator`` already
    # follows). Without this, each fringe-benefit line opened its OWN
    # fresh ``ReferenceSession`` for the identical ``effective_date``
    # instead of reusing the row already fetched here.
    rates = await resolve_ee_rates(effective_date)

    result: EEPayrollResult = await compute_ee_payroll(
        gross=gross,
        effective_date=effective_date,
        pillar_ii_rate_percent=employee.ee_pillar_ii_rate_percent,
        basic_exemption_elected=employee.ee_basic_exemption_elected,
        pensionable_age=employee.ee_pensionable_age,
        rates=rates,
    )

    deductions_total = sum(
        (Decimal(str(d.amount)) for d in line_input.deductions),
        start=Decimal("0"),
    )
    # Net pay deducts the EMPLOYEE-side components only (income tax,
    # employee unemployment, pillar II) — social tax and employer
    # unemployment are pure employer costs, never withheld from net pay.
    net = _q(
        gross - result.income_tax - result.unemployment_employee
        - result.pillar_ii - deductions_total
    )

    # EE fringe-benefit compute (Packet 2) — a SEPARATE tax event from
    # the wage-withholding block above; deliberately NOT folded into
    # ``net`` (module docstring: benefit income tax + social tax are
    # both borne by the EMPLOYER, with no effect on the employee's net
    # pay — services.fringe_benefits_ee module docstring).
    fringe_results: list[EEFringeBenefitResult] = []
    car_rates: CarBenefitRates | None = None
    for fb in line_input.fringe_benefits:
        if fb.engine_power_kw is not None:
            if car_rates is None:
                car_rates = await resolve_car_benefit_rates(effective_date)
            fringe_results.append(
                await compute_car_fringe_benefit(
                    engine_power_kw=fb.engine_power_kw,
                    car_age_years=fb.car_age_years,  # type: ignore[arg-type]
                    effective_date=effective_date,
                    tax_rates=rates,
                    car_rates=car_rates,
                )
            )
        else:
            fringe_results.append(
                await compute_cash_fringe_benefit(
                    benefit_category=fb.benefit_category,
                    taxable_value=fb.taxable_value,  # type: ignore[arg-type]
                    effective_date=effective_date,
                    tax_rates=rates,
                )
            )
    fringe_income_tax = (
        sum((r.income_tax for r in fringe_results), Decimal("0"))
        if fringe_results else None
    )
    fringe_social_tax = (
        sum((r.social_tax for r in fringe_results), Decimal("0"))
        if fringe_results else None
    )

    return ComputedPayLine(
        employee_id=employee.id,
        gross=gross,
        # AU columns explicitly zeroed, not left stale — see
        # ComputedPayLine's EE-block comment.
        payg=Decimal("0"),
        super_amount=Decimal("0"),
        net=net,
        ordinary_hours=Decimal(str(line_input.ordinary_hours)),
        overtime_hours=Decimal(str(line_input.overtime_hours)),
        allowances=[dataclasses.asdict(a) for a in line_input.allowances],
        deductions=[dataclasses.asdict(d) for d in line_input.deductions],
        paid_leave=[],
        lump_sums=[],
        ote=Decimal("0"),
        payg_breakdown="EE path — see ee_income_tax (services.payroll_ee).",
        super_breakdown="EE path — see ee_social_tax/ee_pillar_ii (services.payroll_ee).",
        ee_income_tax=result.income_tax,
        ee_unemployment_employee=result.unemployment_employee,
        ee_unemployment_employer=result.unemployment_employer,
        ee_social_tax=result.social_tax,
        ee_pillar_ii=result.pillar_ii,
        ee_fringe_benefits=[_fringe_benefit_to_dict(r) for r in fringe_results],
        ee_fringe_benefit_income_tax=fringe_income_tax,
        ee_fringe_benefit_social_tax=fringe_social_tax,
    )


async def _compute(
    session: AsyncSession,
    *,
    employee: Employee,
    line_input: PayLineInput,
    effective_date: date,
    period_start: date,
    period_end: date,
    pay_run_id: uuid.UUID,
    company_jurisdiction: str = "AU",
) -> ComputedPayLine:
    """Run withholding + retirement compute for one employee line.
    Pure (no writes).

    Jurisdiction-module Phase 0: the statutory compute now dispatches
    through the neutral payroll seam —
    ``payroll.get_payroll_engine(company_jurisdiction)`` — mirroring
    ``journal._apply_tax_treatment``'s tax dispatch. ``"AU"`` resolves
    to ``jurisdictions.au.payroll.AUPayrollEngine`` (Phase 1 moved it
    into the AU module beside its compute), which makes the exact
    ``payg.compute_withholding`` + ``super_calc.compute_super`` calls
    that used to be inlined here (same inputs, same rounding, same
    breakdown notes — byte-identical AU results, reached through the
    seam). The neutral gross/OTE assembly arithmetic below is plain
    bookkeeping shared by every jurisdiction and stays here. An
    unregistered jurisdiction (including the reserved ``"XX"``
    sentinel) degrades to ``NeutralPayrollEngine`` — net = gross minus
    deductions, zero statutory components.

    ``"EE"`` keeps its pre-seam dedicated branch (``_compute_ee``,
    Packet 3) — formalising EE as a registered module is Phase 5 of
    the jurisdiction-module design.
    """
    if company_jurisdiction == "EE":
        return await _compute_ee(
            session, employee=employee, line_input=line_input, effective_date=effective_date,
            period_start=period_start, period_end=period_end, pay_run_id=pay_run_id,
        )
    period = employee.pay_frequency

    ordinary_rate = _resolve_ordinary_rate(employee, line_input.ordinary_rate)
    ordinary_pay = _q(line_input.ordinary_hours * ordinary_rate)
    ot_rate = _q(ordinary_rate * line_input.overtime_multiplier)
    overtime_pay = _q(line_input.overtime_hours * ot_rate)

    paid_leave_pay = sum(
        (Decimal(str(pl.amount)) for pl in line_input.paid_leave),
        start=Decimal("0"),
    )
    allowance_taxable = sum(
        (Decimal(str(a.amount)) for a in line_input.allowances if a.is_taxable),
        start=Decimal("0"),
    )
    allowance_ote = sum(
        (Decimal(str(a.amount)) for a in line_input.allowances if a.is_ote),
        start=Decimal("0"),
    )
    paid_leave_ote = sum(
        (Decimal(str(pl.amount)) for pl in line_input.paid_leave if pl.is_ote),
        start=Decimal("0"),
    )
    lump_taxable = sum(
        (Decimal(str(ls.amount)) for ls in line_input.lump_sums if ls.is_taxable),
        start=Decimal("0"),
    )

    deductions_total = sum(
        (Decimal(str(d.amount)) for d in line_input.deductions),
        start=Decimal("0"),
    )

    # PAYG gross = everything taxable.
    gross = _q(
        ordinary_pay + overtime_pay + paid_leave_pay
        + allowance_taxable + lump_taxable
    )

    # OTE = ordinary + OTE-flagged allowances + OTE-flagged paid-leave.
    # Crucially: NOT overtime (excluded by SGR 2009/2 r 7).
    # Lump sums default to non-OTE per SGR 2009/2.
    ote = _q(ordinary_pay + allowance_ote + paid_leave_ote)

    engine = get_payroll_engine(company_jurisdiction)
    result = await engine.compute_line(
        session,
        PayrollContext(
            company_id=employee.company_id,
            employee_id=employee.id,
            pay_run_id=pay_run_id,
            period=period,
            period_start=period_start,
            period_end=period_end,
            effective_date=effective_date,
            gross=gross,
            ote=ote,
            deductions_total=deductions_total,
            employee=employee,
            medicare_exemption=line_input.medicare_exemption,
        ),
    )
    # Role-tagged components back to the v2 line columns. For AU these
    # are exactly wh.payg_amount / sg.sg_amount / the same breakdown
    # notes as the pre-seam inline calls — see jurisdictions/au/payroll.py.
    payg = result.total_for(PayrollComponentRole.WITHHOLDING_LIABILITY)
    super_amount = result.total_for(PayrollComponentRole.RETIREMENT_LIABILITY)

    return ComputedPayLine(
        employee_id=employee.id,
        gross=result.gross,
        payg=payg,
        super_amount=super_amount,
        net=result.net,
        ordinary_hours=Decimal(str(line_input.ordinary_hours)),
        overtime_hours=Decimal(str(line_input.overtime_hours)),
        allowances=[dataclasses.asdict(a) for a in line_input.allowances],
        deductions=[dataclasses.asdict(d) for d in line_input.deductions],
        paid_leave=[dataclasses.asdict(p) for p in line_input.paid_leave],
        lump_sums=[dataclasses.asdict(ls) for ls in line_input.lump_sums],
        ote=ote,
        payg_breakdown=result.note_for(PayrollComponentRole.WITHHOLDING_LIABILITY),
        super_breakdown=result.note_for(PayrollComponentRole.RETIREMENT_LIABILITY),
    )


# --------------------------------------------------------------------- #
# YTD                                                                   #
# --------------------------------------------------------------------- #


async def _ytd_for_employee(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    fy_start: date,
    fy_end: date,
    exclude_pay_run_id: uuid.UUID,
) -> tuple[Decimal, Decimal, Decimal]:
    """Sum gross/tax/super for this employee in this FY, excluding this run.

    Returns ``(ytd_gross, ytd_tax, ytd_super)`` BEFORE the current
    line is added. Caller adds the current line's values for the
    post-line YTD.
    """
    # We query pay_run_lines via raw column expressions to dodge
    # the v1 ORM model (which doesn't yet declare the YTD columns).
    stmt = (
        select(
            func.coalesce(func.sum(PayRunLine.gross), Decimal("0")),
            func.coalesce(func.sum(PayRunLine.tax), Decimal("0")),
            func.coalesce(func.sum(PayRunLine.super_amount), Decimal("0")),
        )
        .select_from(PayRunLine)
        .join(PayRun, PayRun.id == PayRunLine.pay_run_id)
        .where(
            PayRun.company_id == company_id,
            PayRunLine.employee_id == employee_id,
            PayRun.archived_at.is_(None),
            PayRun.payment_date >= fy_start,
            PayRun.payment_date <= fy_end,
            PayRun.id != exclude_pay_run_id,
        )
    )
    result = await session.execute(stmt)
    g, t, s = result.one()
    return (_q(g or 0), _q(t or 0), _q(s or 0))


def _fy_bounds(d: date) -> tuple[date, date]:
    if d.month >= 7:
        return date(d.year, 7, 1), date(d.year + 1, 6, 30)
    return date(d.year - 1, 7, 1), date(d.year, 6, 30)


# --------------------------------------------------------------------- #
# Idempotent upsert                                                     #
# --------------------------------------------------------------------- #


async def upsert_line(
    session: AsyncSession,
    *,
    pay_run_id: uuid.UUID,
    line_input: PayLineInput,
    tenant_id: uuid.UUID,
    actor: str,
) -> ComputedPayLine:
    """Compute and persist one pay-run line (idempotent per employee).

    Re-running with the same ``(pay_run_id, employee_id)`` replaces
    the existing line with the new calc.
    """
    pay_run = await _get_pay_run(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunV2Error(f"Pay run {pay_run_id} not found")
    if pay_run.status != PayRunStatus.DRAFT:
        raise PayRunV2Error(
            f"Pay run {pay_run_id} is {pay_run.status!r}; only DRAFT accepts "
            "line writes."
        )

    # Critic round 2: close the crash window between journal_svc.post's
    # own internal commit and the later, separate pay_run.status=FINALIZED
    # commit in _finalize_ee (see that function's F3-fix comment). If a
    # POSTED journal already exists for this pay run, status can still
    # read DRAFT on a crashed/cancelled finalize -- refuse the line write
    # here too (not just the status guard above) so a retried finalize's
    # idempotency-recovery block never reattaches a journal that no
    # longer matches this pay run's line data.
    stale_journal = (
        await session.execute(
            select(JournalEntry.id).where(
                JournalEntry.source_type == "pay_run",
                JournalEntry.source_id == pay_run.id,
                JournalEntry.status == EntryStatus.POSTED,
            )
        )
    ).scalars().first()
    if stale_journal is not None:
        raise PayRunV2Error(
            f"Pay run {pay_run_id} already has a posted journal "
            f"({stale_journal}) from an interrupted finalize; it cannot "
            "accept line writes. Retry finalize to recover it, or void "
            "the pay run first."
        )

    employee = await session.get(Employee, line_input.employee_id)
    if employee is None or employee.company_id != pay_run.company_id:
        raise PayRunV2Error(
            f"Employee {line_input.employee_id} not found for this company"
        )

    company_jurisdiction = (
        await session.execute(
            select(Company.jurisdiction).where(Company.id == pay_run.company_id)
        )
    ).scalar_one_or_none() or "AU"

    computed = await _compute(
        session,
        employee=employee,
        line_input=line_input,
        effective_date=pay_run.payment_date,
        period_start=pay_run.period_start,
        period_end=pay_run.period_end,
        pay_run_id=pay_run.id,
        company_jurisdiction=company_jurisdiction,
    )

    # Delete any existing line for this (pay_run, employee).
    await session.execute(
        delete(PayRunLine).where(
            PayRunLine.pay_run_id == pay_run_id,
            PayRunLine.employee_id == line_input.employee_id,
        )
    )

    # YTD running totals (post-line).
    fy_start, fy_end = _fy_bounds(pay_run.payment_date)
    pre_g, pre_t, pre_s = await _ytd_for_employee(
        session,
        company_id=pay_run.company_id,
        employee_id=line_input.employee_id,
        fy_start=fy_start,
        fy_end=fy_end,
        exclude_pay_run_id=pay_run_id,
    )
    ytd_gross = _q(pre_g + computed.gross)
    ytd_tax = _q(pre_t + computed.payg)
    ytd_super = _q(pre_s + computed.super_amount)

    # Insert the new line. Phase 1B adds the extended columns; we
    # set them via raw INSERT so this code compiles even before the
    # ORM class catches up.
    insert_stmt = insert(PayRunLine.__table__).values(
        id=uuid.uuid4(),
        pay_run_id=pay_run_id,
        # 0129_pay_runs_rls added tenant_id NOT NULL + RLS to this table
        # after this INSERT was written; pre-existing gap, fixed forward
        # here (see PayRunLine.tenant_id's docstring in models/pay_run.py).
        tenant_id=pay_run.tenant_id,
        employee_id=line_input.employee_id,
        gross=computed.gross,
        tax=computed.payg,
        super_amount=computed.super_amount,
        net=computed.net,
    )
    # Phase 1B extended columns: add to the INSERT only if the table
    # actually has them. ``PayRunLine.__table__.columns`` is the
    # authoritative source post-migration.
    extra_cols: dict[str, Any] = {
        "ordinary_hours": computed.ordinary_hours,
        "overtime_hours": computed.overtime_hours,
        "allowances": computed.allowances,
        "deductions": computed.deductions,
        "paid_leave": computed.paid_leave,
        "lump_sums": computed.lump_sums,
        "ytd_gross": ytd_gross,
        "ytd_tax": ytd_tax,
        "ytd_super": ytd_super,
        # EE payroll compute (Packet 3) — None on every AU line, so this
        # is a no-op INSERT of NULL for AU (present_cols gate below still
        # applies pre-migration/pre-Packet-3).
        "ee_income_tax": computed.ee_income_tax,
        "ee_unemployment_employee": computed.ee_unemployment_employee,
        "ee_unemployment_employer": computed.ee_unemployment_employer,
        "ee_social_tax": computed.ee_social_tax,
        "ee_pillar_ii": computed.ee_pillar_ii,
        # EE fringe-benefit compute (Packet 2) — [] / None for every AU
        # line and every EE line with no fringe_benefits input.
        "ee_fringe_benefits": computed.ee_fringe_benefits,
        "ee_fringe_benefit_income_tax": computed.ee_fringe_benefit_income_tax,
        "ee_fringe_benefit_social_tax": computed.ee_fringe_benefit_social_tax,
    }
    present_cols = set(PayRunLine.__table__.columns.keys())
    extras_to_apply = {
        k: v for k, v in extra_cols.items() if k in present_cols
    }
    if extras_to_apply:
        insert_stmt = insert_stmt.values(**extras_to_apply)
    await session.execute(insert_stmt)

    await cl_svc.append(
        session,
        entity="pay_run_line",
        entity_id=pay_run_id,  # parent — the line id is opaque to caller
        op="update",
        actor=actor,
        payload={
            "employee_id": str(line_input.employee_id),
            "gross": str(computed.gross),
            "payg": str(computed.payg),
            "super": str(computed.super_amount),
            "net": str(computed.net),
            "payg_breakdown": computed.payg_breakdown,
            "super_breakdown": computed.super_breakdown,
        },
        version=1,
    )

    await session.commit()
    return computed


# --------------------------------------------------------------------- #
# Finalize (per-employee JE)                                            #
# --------------------------------------------------------------------- #


def _line_role_amounts(
    line: PayRunLine,
) -> dict[PayrollComponentRole, Decimal]:
    """Reconstruct the role-tagged statutory amounts a stored line carries.

    The v2 line columns ARE the role snapshot ``_compute`` wrote from
    the engine's ``PayrollResult`` (``tax`` ← WITHHOLDING_LIABILITY,
    ``super_amount`` ← RETIREMENT_LIABILITY), so finalize posts from
    the stored snapshot — never a recompute (rates may have moved
    between draft and finalize). The employer-funded retirement
    contribution always pairs an expense leg with its liability (the
    engine emits both roles with the same amount — see
    ``jurisdictions.au.payroll``), so RETIREMENT_EXPENSE mirrors
    ``super_amount``. Roles the v2 line schema has no column for (the
    EMPLOYER_SOCIAL_* pair) cannot appear until the schema grows them.
    """
    withholding = line.tax or Decimal("0")
    retirement = line.super_amount or Decimal("0")
    return {
        PayrollComponentRole.WITHHOLDING_LIABILITY: withholding,
        PayrollComponentRole.RETIREMENT_LIABILITY: retirement,
        PayrollComponentRole.RETIREMENT_EXPENSE: retirement,
    }


async def _get_pay_run(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> PayRun | None:
    stmt = select(PayRun).where(
        PayRun.id == pay_run_id,
        PayRun.archived_at.is_(None),
    )
    if tenant_id is not None:
        stmt = stmt.where(PayRun.tenant_id == tenant_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def finalize_with_je(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    actor: str,
) -> PayRun:
    """Build the per-employee JE and post it; mark the run FINALIZED.

    Jurisdiction-module Phase 1: the JE is posted **generically from
    the role-tagged statutory amounts** each line carries, mapped to
    accounts by the jurisdiction's payroll posting profile
    (``payroll.get_posting_profile``) — the old hardcoded
    AU-if/else JE shape is gone. Per-employee shape:

        Dr Wages expense                    gross
        Dr each ``*_EXPENSE`` role account     (AU: SG expense = sg)
           Cr each ``*_LIABILITY`` role account   (AU: PAYG WH = payg,
                                                    Super payable = sg)
           Cr Bank (net-pay clearing)       net

    For AU (``jurisdictions.au.PAYROLL_POSTING``) this produces the
    exact pre-Phase-1 5-leg JE — same accounts, same order, same
    descriptions, same amounts. A jurisdiction with no payroll module
    degrades to the neutral profile (wages + net only) instead of the
    old hard refusal; EE keeps its dedicated ``_finalize_ee`` branch
    (formalising EE onto this path is Phase 5).

    A pay run with N employees produces a single JE with N×5 lines
    (or fewer if some employees have $0 super / no PAYG). All lines
    share one ``ref`` to keep the GL legible.
    """
    pay_run = await _get_pay_run(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunV2Error(f"Pay run {pay_run_id} not found")
    if pay_run.status == PayRunStatus.FINALIZED:
        # Fixer round 5 (F3): a genuinely-posted pay run (journal_id set,
        # every path that flips FINALIZED here or in ``_finalize_ee`` sets
        # it in the same commit) is a real no-op — return it unchanged.
        # ``finalize_ee_status_only`` also flips status to FINALIZED but
        # deliberately leaves journal_id None (TSD-only status lock, no GL
        # posting — see its own docstring). Without this split, that
        # combination made this short-circuit swallow the call silently
        # forever: no journal was ever posted and no error was raised.
        # Raise loudly instead — same tax-safe posture as ``_compute_ee``'s
        # own guards — rather than pretend the pay run is fully finalized.
        if pay_run.journal_id is not None:
            return pay_run
        raise PayRunV2Error(
            f"Pay run {pay_run_id} is already FINALIZED but has no "
            "journal_id — it was locked via finalize_ee_status_only "
            "(status-only lock for TSD sourcing, no GL posting) and "
            "finalize_with_je cannot silently no-op past that: no "
            "wages/tax journal has ever been posted for this period. "
            "Void the pay run and re-finalize, rather than retrying "
            "finalize_with_je on an already status-locked pay run."
        )
    if pay_run.status not in (PayRunStatus.DRAFT, PayRunStatus.ABA_EXPORTED):
        raise PayRunV2Error(
            f"Cannot finalize from status {pay_run.status!r}"
        )

    # Resolve accounts once.
    co_id = pay_run.company_id

    # Packet 1 (kmd-inf-tsd follow-up) — EE JE posting IS built now, via
    # ``_finalize_ee`` below. Dispatch BEFORE any account resolution so
    # the generic path beneath stays byte-identical to the pre-Phase-1
    # AU branch for an AU-jurisdiction company.
    company_jurisdiction = (
        await session.execute(
            select(Company.jurisdiction).where(Company.id == co_id)
        )
    ).scalar_one_or_none() or "AU"
    if company_jurisdiction == "EE":
        return await _finalize_ee(session, pay_run, tenant_id=tenant_id, actor=actor)

    # Generic role-tagged posting (jurisdiction-module Phase 1). The
    # profile's ``role_accounts`` order is contractual: it fixes both
    # which missing-account error fires first (wages → roles → net,
    # matching the old AU wages/super-exp/payg/super-liab/net order)
    # and the per-employee JE leg order below.
    profile = get_posting_profile(company_jurisdiction)
    wages = await _account_by_code(
        session, company_id=co_id, code=profile.wages_account_code
    )
    role_accounts: list[tuple[PayrollRoleAccount, Account]] = []
    for spec in profile.role_accounts:
        acct = await _account_by_code(
            session, company_id=co_id, code=spec.account_code
        )
        role_accounts.append((spec, acct))
    net_clearing = await _account_by_code(
        session, company_id=co_id, code=profile.net_account_code
    )

    # Fetch lines.
    lines_result = await session.execute(
        select(PayRunLine).where(PayRunLine.pay_run_id == pay_run_id)
    )
    lines = list(lines_result.scalars().all())
    if not lines:
        raise PayRunV2Error("Pay run has no lines — add at least one before finalize.")

    je_lines: list[dict[str, Any]] = []
    mapped_roles = {spec.role for spec, _acct in role_accounts}
    for line in lines:
        emp_label = str(line.employee_id)[:8]
        amounts = _line_role_amounts(line)
        unmapped = [
            role.value
            for role, amount in amounts.items()
            if amount > 0 and role not in mapped_roles
        ]
        if unmapped:
            # Tax-safe posture: a statutory amount with nowhere to book
            # must refuse loudly, never silently drop the leg (which
            # would also unbalance the JE against net).
            raise PayRunV2Error(
                f"Pay run line for employee {line.employee_id} carries "
                f"statutory amounts with no account mapping in the "
                f"{company_jurisdiction!r} payroll posting profile: "
                f"{', '.join(sorted(unmapped))}. The company's "
                "jurisdiction has no payroll module registered for "
                "these roles — recompute the lines under the correct "
                "jurisdiction before finalizing."
            )
        # Dr Wages
        if line.gross > 0:
            je_lines.append({
                "account_id": wages.id,
                "description": f"{profile.wages_label}: {emp_label}",
                "debit": line.gross,
                "credit": Decimal("0"),
            })
        # Role-tagged statutory legs, in profile order — for AU:
        # Dr SG expense / Cr PAYG WH / Cr Super payable, exactly the
        # pre-Phase-1 hardcoded shape.
        for spec, acct in role_accounts:
            amount = amounts.get(spec.role, Decimal("0"))
            if amount <= 0:
                continue
            debit = spec.role.posts_debit
            je_lines.append({
                "account_id": acct.id,
                "description": f"{spec.label}: {emp_label}",
                "debit": amount if debit else Decimal("0"),
                "credit": Decimal("0") if debit else amount,
            })
        # Cr Net pay clearing
        if line.net > 0:
            je_lines.append({
                "account_id": net_clearing.id,
                "description": f"{profile.net_label}: {emp_label}",
                "debit": Decimal("0"),
                "credit": line.net,
            })

    entry = await journal_svc.create_draft(
        session,
        company_id=co_id,
        entry_date=pay_run.payment_date,
        description=(
            f"Payroll {pay_run.period_start} to {pay_run.period_end} "
            f"({len(lines)} employee{'s' if len(lines) != 1 else ''})"
        ),
        lines=je_lines,
        tenant_id=tenant_id,
    )
    try:
        await journal_svc.post(
            session, entry.id, posted_by=actor, tenant_id=tenant_id,
            origin=JournalOrigin.PAYRUN,
            source_type="pay_run",
            source_id=pay_run.id,
        )
    except journal_svc.PostingError as exc:
        raise PayRunV2Error(f"Journal post failed: {exc}") from exc

    pay_run.journal_id = entry.id
    pay_run.status = PayRunStatus.FINALIZED
    pay_run.version += 1
    pay_run.updated_at = datetime.utcnow()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="update",
        actor=actor,
        payload={
            "journal_id": str(entry.id),
            "line_count": len(lines),
            "total_gross": str(sum((ln.gross for ln in lines), Decimal("0"))),
            "total_payg": str(sum((ln.tax for ln in lines), Decimal("0"))),
            "total_super": str(sum((ln.super_amount for ln in lines), Decimal("0"))),
            "total_net": str(sum((ln.net for ln in lines), Decimal("0"))),
        },
        version=pay_run.version,
    )
    await session.commit()

    # Payday Super Phase 1 — best-effort lodgement build. Gated by
    # SAEBOOKS_PAYDAY_SUPER / SAEBOOKS_ENV. Failures are logged and
    # swallowed; the pay-run finalise must not roll back if super
    # lodgement generation fails.
    from saebooks.services.super_stream import maybe_build_after_finalize

    await maybe_build_after_finalize(
        session,
        tenant_id=tenant_id,
        company_id=pay_run.company_id,
        pay_run_id=pay_run.id,
    )
    return pay_run


async def _finalize_ee(
    session: AsyncSession,
    pay_run: PayRun,
    *,
    tenant_id: uuid.UUID,
    actor: str,
) -> PayRun:
    """Build and post the EE pay-run finalize JE (Packet 1 — closes the
    gap ``finalize_with_je`` used to hard-refuse for EE). Per-employee
    shape:

        Dr Wages expense                 gross
        Dr Social tax expense            social_tax   (33%, incl. the
                                                        EUR 886/mo floor)
        Dr Employer unemployment expense unemployment_employer (0.8%)
           Cr Income tax payable             income_tax
           Cr Unemployment payable (employee)  unemployment_employee (1.6%)
           Cr Pillar II payable              pillar_ii
           Cr Social tax payable             social_tax
           Cr Employer unemployment payable  unemployment_employer
           Cr Net wages payable              net

    Plus, per-line, IFF that line carries a fringe benefit (Packet 2 —
    ``services.fringe_benefits_ee``; the benefit's own VALUE is never
    posted, only its two tax consequences, both pure employer cost with
    NO net-pay effect):

        Dr Fringe benefit income tax expense  fb_income_tax
        Dr Fringe benefit social tax expense   fb_social_tax
           Cr Fringe benefit income tax payable  fb_income_tax
           Cr Fringe benefit social tax payable  fb_social_tax

    Employee-side amounts (income tax, unemployment employee, pillar II)
    are already inside ``gross`` — they book straight to a liability
    with no matching expense leg, same as AU's PAYG WH. Employer-side
    amounts (social tax, employer unemployment) are additional employer
    cost — each gets an expense leg AND a liability leg, per the packet.

    Account resolution is per-company-column-driven
    (``_account_by_company_column``, 0200 — the same per-company
    override pattern 0198 established for AR/AP control accounts) — EE
    has no chart-template seed to hardcode fixed codes against, unlike
    AU's ``_ACCT_*`` constants above.

    ⚠ Deductions are NOT booked to a liability leg — mirrors the
    pre-existing gap in the AU branch above (``net`` already subtracts
    ``deductions_total`` with no offsetting credit line anywhere in
    either branch). For the golden month (no deductions) this is a
    no-op; if a caller ever populates ``PayLineInput.deductions`` for an
    EE line, ``journal_svc.post``'s balance check refuses the JE loudly
    rather than post a silently-unbalanced one.

    ``finalize_ee_status_only`` (below) predates this function — it
    exists only because this function used to hard-refuse EE. It is now
    SUPERSEDED for real use (this function is the canonical EE
    finalize), but is left byte-unchanged: the TSD golden tests
    (``tests/services/lodgement/test_tsd_golden.py`` /
    ``test_tsd_generator.py``) call it directly and their fixtures are
    pinned to its no-journal behaviour. Migrating those callers onto
    this function (adding the 9 settings-keyed payroll accounts to
    their shared ``_make_ee_company`` fixture) is a follow-up, not this
    packet.
    """
    co_id = pay_run.company_id

    # Fixer round 1 (F3): finalize_with_je flips pay_run.status/journal_id
    # to FINALIZED under a SEPARATE, later commit than journal_svc.post's
    # own internal commit — if that later commit never lands (crash,
    # cancelled request, cl_svc.append error) the pay run is retried from
    # DRAFT with no memory of the JE it already posted, producing a SECOND
    # fully-posted journal. Recover idempotently instead: if a POSTED
    # journal already exists for this pay run, attach it and return rather
    # than building a duplicate.
    existing = (
        await session.execute(
            select(JournalEntry).where(
                JournalEntry.source_type == "pay_run",
                JournalEntry.source_id == pay_run.id,
                JournalEntry.status == EntryStatus.POSTED,
            )
        )
    ).scalars().first()
    if existing is not None:
        pay_run.journal_id = existing.id
        pay_run.status = PayRunStatus.FINALIZED
        pay_run.version += 1
        pay_run.updated_at = datetime.utcnow()
        await cl_svc.append(
            session,
            entity="pay_run",
            entity_id=pay_run.id,
            op="update",
            actor=actor,
            payload={
                "journal_id": str(existing.id),
                "note": "Recovered pre-existing POSTED journal on retry (F3 fix).",
            },
            version=pay_run.version,
        )
        await session.commit()
        return pay_run

    lines_result = await session.execute(
        select(PayRunLine).where(PayRunLine.pay_run_id == pay_run.id)
    )
    lines = list(lines_result.scalars().all())
    if not lines:
        raise PayRunV2Error("Pay run has no lines — add at least one before finalize.")

    # Fixer round 5 (F1): resolve each base wage-related account ONLY if
    # some line actually needs it — mirrors the fringe-benefit columns'
    # own conditional resolution just below. A director paid entirely via
    # a fringe benefit (gross=0 on every line, only FringeBenefitLine
    # populated) has no reason to have ANY of the 9 base wage columns
    # configured; resolving them unconditionally made
    # ``PayRunV2Error`` fire before ``je_lines`` was ever built, even
    # though every base leg below is itself guarded by the matching
    # `if ... > 0` check and would never be emitted.
    has_wages = any(ln.gross > 0 for ln in lines)
    has_social_tax = any((ln.ee_social_tax or Decimal("0")) > 0 for ln in lines)
    has_unemployment_employer = any(
        (ln.ee_unemployment_employer or Decimal("0")) > 0 for ln in lines
    )
    has_income_tax = any((ln.ee_income_tax or Decimal("0")) > 0 for ln in lines)
    has_unemployment_employee = any(
        (ln.ee_unemployment_employee or Decimal("0")) > 0 for ln in lines
    )
    has_pillar_ii = any((ln.ee_pillar_ii or Decimal("0")) > 0 for ln in lines)
    has_net = any(ln.net > 0 for ln in lines)

    wages = (
        await _account_by_company_column(
            session, company_id=co_id, column_name=_EE_WAGES_EXPENSE_COLUMN
        )
        if has_wages else None
    )
    social_tax_exp = (
        await _account_by_company_column(
            session, company_id=co_id, column_name=_EE_SOCIAL_TAX_EXPENSE_COLUMN
        )
        if has_social_tax else None
    )
    unemp_er_exp = (
        await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_UNEMPLOYMENT_EMPLOYER_EXPENSE_COLUMN,
        )
        if has_unemployment_employer else None
    )
    income_tax_liab = (
        await _account_by_company_column(
            session, company_id=co_id, column_name=_EE_INCOME_TAX_PAYABLE_COLUMN
        )
        if has_income_tax else None
    )
    unemp_ee_liab = (
        await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_UNEMPLOYMENT_EMPLOYEE_PAYABLE_COLUMN,
        )
        if has_unemployment_employee else None
    )
    pillar_ii_liab = (
        await _account_by_company_column(
            session, company_id=co_id, column_name=_EE_PILLAR_II_PAYABLE_COLUMN
        )
        if has_pillar_ii else None
    )
    social_tax_liab = (
        await _account_by_company_column(
            session, company_id=co_id, column_name=_EE_SOCIAL_TAX_PAYABLE_COLUMN
        )
        if has_social_tax else None
    )
    unemp_er_liab = (
        await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_UNEMPLOYMENT_EMPLOYER_PAYABLE_COLUMN,
        )
        if has_unemployment_employer else None
    )
    net_clearing = (
        await _account_by_company_column(
            session, company_id=co_id, column_name=_EE_NET_PAY_CLEARING_COLUMN
        )
        if has_net else None
    )

    # Fringe-benefit (erisoodustus) tax legs — Packet 2. Resolved ONLY if
    # at least one line actually carries a nonzero fringe-benefit total
    # (see the settings-key block's own comment: these 4 keys must not
    # become a hard finalize blocker for a company that never grants a
    # fringe benefit — every pre-Packet-2 EE company has none of them
    # configured).
    has_fringe_benefits = any(
        (ln.ee_fringe_benefit_income_tax or Decimal("0")) > 0
        or (ln.ee_fringe_benefit_social_tax or Decimal("0")) > 0
        for ln in lines
    )
    fringe_income_tax_exp = fringe_social_tax_exp = None
    fringe_income_tax_liab = fringe_social_tax_liab = None
    if has_fringe_benefits:
        fringe_income_tax_exp = await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_FRINGE_BENEFIT_INCOME_TAX_EXPENSE_COLUMN,
        )
        fringe_social_tax_exp = await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_FRINGE_BENEFIT_SOCIAL_TAX_EXPENSE_COLUMN,
        )
        fringe_income_tax_liab = await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_FRINGE_BENEFIT_INCOME_TAX_PAYABLE_COLUMN,
        )
        fringe_social_tax_liab = await _account_by_company_column(
            session, company_id=co_id,
            column_name=_EE_FRINGE_BENEFIT_SOCIAL_TAX_PAYABLE_COLUMN,
        )

    je_lines: list[dict[str, Any]] = []
    for line in lines:
        emp_label = str(line.employee_id)[:8]
        income_tax = line.ee_income_tax or Decimal("0")
        unemployment_employee = line.ee_unemployment_employee or Decimal("0")
        unemployment_employer = line.ee_unemployment_employer or Decimal("0")
        social_tax = line.ee_social_tax or Decimal("0")
        pillar_ii = line.ee_pillar_ii or Decimal("0")

        if line.gross > 0:
            je_lines.append({
                "account_id": wages.id,
                "description": f"Wages: {emp_label}",
                "debit": line.gross, "credit": Decimal("0"),
            })
        if social_tax > 0:
            je_lines.append({
                "account_id": social_tax_exp.id,
                "description": f"Social tax (employer): {emp_label}",
                "debit": social_tax, "credit": Decimal("0"),
            })
        if unemployment_employer > 0:
            je_lines.append({
                "account_id": unemp_er_exp.id,
                "description": f"Unemployment (employer): {emp_label}",
                "debit": unemployment_employer, "credit": Decimal("0"),
            })
        if income_tax > 0:
            je_lines.append({
                "account_id": income_tax_liab.id,
                "description": f"Income tax withheld: {emp_label}",
                "debit": Decimal("0"), "credit": income_tax,
            })
        if unemployment_employee > 0:
            je_lines.append({
                "account_id": unemp_ee_liab.id,
                "description": f"Unemployment (employee): {emp_label}",
                "debit": Decimal("0"), "credit": unemployment_employee,
            })
        if pillar_ii > 0:
            je_lines.append({
                "account_id": pillar_ii_liab.id,
                "description": f"Pillar II: {emp_label}",
                "debit": Decimal("0"), "credit": pillar_ii,
            })
        if social_tax > 0:
            je_lines.append({
                "account_id": social_tax_liab.id,
                "description": f"Social tax payable: {emp_label}",
                "debit": Decimal("0"), "credit": social_tax,
            })
        if unemployment_employer > 0:
            je_lines.append({
                "account_id": unemp_er_liab.id,
                "description": f"Unemployment payable (employer): {emp_label}",
                "debit": Decimal("0"), "credit": unemployment_employer,
            })
        if line.net > 0:
            je_lines.append({
                "account_id": net_clearing.id,
                "description": f"Net pay: {emp_label}",
                "debit": Decimal("0"), "credit": line.net,
            })

        # Fringe-benefit (erisoodustus) tax legs — Packet 2. The benefit's
        # own VALUE (e.g. the car's running costs) is NOT posted here —
        # only its two tax consequences, both pure employer cost with NO
        # net-pay effect (module docstring of services.fringe_benefits_ee).
        fb_income_tax = line.ee_fringe_benefit_income_tax or Decimal("0")
        fb_social_tax = line.ee_fringe_benefit_social_tax or Decimal("0")
        if fb_income_tax > 0:
            je_lines.append({
                "account_id": fringe_income_tax_exp.id,
                "description": f"Fringe benefit income tax: {emp_label}",
                "debit": fb_income_tax, "credit": Decimal("0"),
            })
            je_lines.append({
                "account_id": fringe_income_tax_liab.id,
                "description": f"Fringe benefit income tax payable: {emp_label}",
                "debit": Decimal("0"), "credit": fb_income_tax,
            })
        if fb_social_tax > 0:
            je_lines.append({
                "account_id": fringe_social_tax_exp.id,
                "description": f"Fringe benefit social tax: {emp_label}",
                "debit": fb_social_tax, "credit": Decimal("0"),
            })
            je_lines.append({
                "account_id": fringe_social_tax_liab.id,
                "description": f"Fringe benefit social tax payable: {emp_label}",
                "debit": Decimal("0"), "credit": fb_social_tax,
            })

    entry = await journal_svc.create_draft(
        session,
        company_id=co_id,
        entry_date=pay_run.payment_date,
        description=(
            f"EE Payroll {pay_run.period_start} to {pay_run.period_end} "
            f"({len(lines)} employee{'s' if len(lines) != 1 else ''})"
        ),
        lines=je_lines,
        tenant_id=tenant_id,
    )
    try:
        await journal_svc.post(
            session, entry.id, posted_by=actor, tenant_id=tenant_id,
            origin=JournalOrigin.PAYRUN,
            source_type="pay_run",
            source_id=pay_run.id,
        )
    except journal_svc.PostingError as exc:
        # F1 fix: create_draft already committed ``entry`` (it internally
        # commits — journal.py's create_draft), so a post() failure (e.g.
        # an unbalanced JE from the documented "deductions not booked to a
        # liability leg" gap above) would otherwise leave a permanently
        # orphaned DRAFT journal entry, consuming a ref sequence number on
        # every retry. post_in_txn's balance check runs BEFORE any status
        # mutation, so ``entry`` is still DRAFT here and safe to delete.
        await journal_svc.delete(
            session, entry.id, performed_by=actor, tenant_id=tenant_id,
            company_id=co_id,
        )
        raise PayRunV2Error(f"Journal post failed: {exc}") from exc

    pay_run.journal_id = entry.id
    pay_run.status = PayRunStatus.FINALIZED
    pay_run.version += 1
    pay_run.updated_at = datetime.utcnow()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="update",
        actor=actor,
        payload={
            "journal_id": str(entry.id),
            "line_count": len(lines),
            "total_gross": str(sum((ln.gross for ln in lines), Decimal("0"))),
            "total_income_tax": str(sum(
                ((ln.ee_income_tax or Decimal("0")) for ln in lines), Decimal("0")
            )),
            "total_social_tax": str(sum(
                ((ln.ee_social_tax or Decimal("0")) for ln in lines), Decimal("0")
            )),
            "total_unemployment_employee": str(sum(
                ((ln.ee_unemployment_employee or Decimal("0")) for ln in lines),
                Decimal("0"),
            )),
            "total_unemployment_employer": str(sum(
                ((ln.ee_unemployment_employer or Decimal("0")) for ln in lines),
                Decimal("0"),
            )),
            "total_pillar_ii": str(sum(
                ((ln.ee_pillar_ii or Decimal("0")) for ln in lines), Decimal("0")
            )),
            "total_fringe_benefit_income_tax": str(sum(
                ((ln.ee_fringe_benefit_income_tax or Decimal("0")) for ln in lines),
                Decimal("0"),
            )),
            "total_fringe_benefit_social_tax": str(sum(
                ((ln.ee_fringe_benefit_social_tax or Decimal("0")) for ln in lines),
                Decimal("0"),
            )),
            "total_net": str(sum((ln.net for ln in lines), Decimal("0"))),
        },
        version=pay_run.version,
    )
    await session.commit()
    return pay_run


async def void_pay_run(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    actor: str,
    override_reason: str | None = None,
) -> PayRun:
    """Reverse a FINALIZED pay run's posted journal entry and mark the
    run VOIDED — Packet 1's reversal story.

    Mirrors ``services.bills.void_bill`` (``journal_svc.reverse``):
    pay_runs_v2 had no prior unfinalize/void path for EITHER
    jurisdiction to mirror byte-for-byte (there was nothing there), so
    this is built generically off ``pay_run.journal_id`` and does not
    branch on jurisdiction — it works for an AU-finalized run too.

    The reversal entry is found afterwards via its own
    ``reversal_of_id == pay_run.journal_id`` (``journal_svc.reverse``
    sets this) — ``pay_run.journal_id`` itself is left pointing at the
    original (now REVERSED) entry, so no new column/migration was
    needed. Callers computing "nets to zero" must include BOTH the
    REVERSED original and its POSTED reversal (mirrors
    ``tests/services/test_tax_return_generator.py``'s
    ``REPORTABLE_STATUSES`` convention) — a POSTED-only query sees only
    the reversal and reads as the negative of the original, not zero.

    Idempotent: voiding an already-VOIDED run is a no-op (returns as-is,
    same convention as ``void_bill``).
    """
    pay_run = await _get_pay_run(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunV2Error(f"Pay run {pay_run_id} not found")
    if pay_run.status == PayRunStatus.VOIDED:
        return pay_run
    if pay_run.status != PayRunStatus.FINALIZED:
        raise PayRunV2Error(
            f"Cannot void pay run {pay_run_id} from status "
            f"{pay_run.status!r} — only a FINALIZED pay run can be voided."
        )
    if pay_run.journal_id is None:
        raise PayRunV2Error(
            f"Pay run {pay_run_id} is FINALIZED but has no journal_id — "
            "nothing to reverse. (A pay run finalized via "
            "finalize_ee_status_only never posts a journal; there is no "
            "GL entry here to void.)"
        )

    # F4 fix: journal_svc.reverse() is internally atomic (its own trailing
    # commit covers both posting the reversal AND flipping the original to
    # REVERSED), but THIS function's pay_run.status flip is a separate,
    # later commit. If that later commit never lands (crash, cancelled
    # request, cl_svc.append error), a retry would call reverse() again on
    # an already-REVERSED original, which raises PostingError uncaught —
    # permanently stuck at FINALIZED with the journal already reversed
    # underneath it. Detect that partially-completed state up front and
    # skip straight to the VOIDED flip instead of re-reversing.
    original = await journal_svc.get(session, pay_run.journal_id, tenant_id=tenant_id)
    if original.status != EntryStatus.REVERSED:
        await journal_svc.reverse(
            session,
            pay_run.journal_id,
            posted_by=actor,
            override_reason=override_reason or f"Void pay run {pay_run.id}",
            tenant_id=tenant_id,
        )
    pay_run.status = PayRunStatus.VOIDED
    pay_run.version += 1
    pay_run.updated_at = datetime.utcnow()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="update",
        actor=actor,
        payload={"note": "Pay run voided; journal reversed (see reversal_of_id)."},
        version=pay_run.version,
    )
    await session.commit()
    return pay_run


async def finalize_ee_status_only(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    actor: str,
) -> PayRun:
    """Lock an EE pay run's lines without posting a journal entry.

    kmd-inf-tsd scope Packet 4 fix-forward, flagged prominently: the
    scope's own §1.2/§2.2 premise is TSD generation "from posted EE
    pay runs", but ``finalize_with_je`` (Packet 3, unmodified above)
    hard-refuses any non-AU jurisdiction — its 5-leg JE shape is
    AU-account-coded and explicitly deferred EE journal posting to "a
    follow-up packet". That leaves **no path at all** for an EE pay
    run to reach ``FINALIZED`` — i.e. the scope's own stated TSD data
    source was unreachable. This is the minimal fix: mirror the
    existing DRAFT->FINALIZED lock semantics (``upsert_line`` already
    refuses further line writes once status leaves DRAFT — see its own
    guard) WITHOUT building the AU-shaped ledger posting, so EE pay
    run lines can become immutable ("posted" in this codebase's sense)
    for the TSD generator to read. Ledger posting for EE payroll
    remains a real, separate gap (no JE, no ``journal_id`` set here) —
    this function does not close it, only unblocks TSD sourcing.
    """
    pay_run = await _get_pay_run(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunV2Error(f"Pay run {pay_run_id} not found")
    if pay_run.status == PayRunStatus.FINALIZED:
        return pay_run
    if pay_run.status != PayRunStatus.DRAFT:
        raise PayRunV2Error(f"Cannot finalize from status {pay_run.status!r}")

    company_jurisdiction = (
        await session.execute(
            select(Company.jurisdiction).where(Company.id == pay_run.company_id)
        )
    ).scalar_one_or_none() or "AU"
    if company_jurisdiction != "EE":
        raise PayRunV2Error(
            "finalize_ee_status_only is for EE-jurisdiction companies only "
            f"— got {company_jurisdiction!r}. AU pay runs use "
            "finalize_with_je."
        )

    lines_result = await session.execute(
        select(PayRunLine).where(PayRunLine.pay_run_id == pay_run_id)
    )
    lines = list(lines_result.scalars().all())
    if not lines:
        raise PayRunV2Error("Pay run has no lines — add at least one before finalize.")

    pay_run.status = PayRunStatus.FINALIZED
    pay_run.version += 1
    pay_run.updated_at = datetime.utcnow()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="update",
        actor=actor,
        payload={
            "journal_id": None,
            "line_count": len(lines),
            "note": (
                "EE status-only finalize — no journal entry posted "
                "(EE ledger posting is a separate, unclosed gap)."
            ),
        },
        version=pay_run.version,
    )
    await session.commit()
    return pay_run


__all__ = [
    "AllowanceLine",
    "ComputedPayLine",
    "DeductionLine",
    "LumpSumLine",
    "PaidLeaveLine",
    "PayLineInput",
    "PayRunV2Error",
    "finalize_ee_status_only",
    "finalize_with_je",
    "upsert_line",
    "void_pay_run",
]
