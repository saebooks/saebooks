"""Pay-run orchestration with PAYG + Super calc + per-employee JE.

This is the Phase 2 successor to ``services/pay_runs.py``. Where the
v1 service treated each ``pay_run_line`` as a hand-entered
``(gross, tax, super, net)`` quadruple and produced one lump-sum JE
per run, the v2 service:

* Takes the **inputs** to a pay-line (ordinary hours, overtime hours,
  allowances, deductions, paid leave, lump sums, OTE) and computes
  the PAYG + super amounts via ``services.payg`` and
  ``services.super_calc``.
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
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import delete, extract, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.employee import Employee, PayFrequency
from saebooks.models.journal import JournalOrigin
from saebooks.models.pay_run import PayRun, PayRunLine, PayRunStatus
from saebooks.services import change_log as cl_svc
from saebooks.services import journal as journal_svc
from saebooks.services.payg import (
    MedicareExemption,
    WithholdingResult,
    compute_withholding,
)
from saebooks.services.payroll_ee import EEPayrollResult, compute_ee_payroll
from saebooks.services.super_calc import SuperResult, compute_super


class PayRunV2Error(ValueError):
    """Domain-level failure during a v2 pay-run operation."""


# Account code conventions. Override via company settings in Phase 2B.
_ACCT_WAGES_EXPENSE = "6-2110"
_ACCT_SUPER_EXPENSE = "6-2120"
_ACCT_PAYG_LIABILITY = "2-1310"
_ACCT_SUPER_LIABILITY = "2-1320"
_ACCT_NET_PAY_CLEARING = "2-1150"


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
    medicare_exemption: MedicareExemption = "NONE"
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


# --------------------------------------------------------------------- #
# Calc                                                                  #
# --------------------------------------------------------------------- #


def _q(value: Decimal | int | float | str) -> Decimal:
    """Quantize a value to 2 dp (half-up)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    scales via ``services.payg``'s period tables), this function never
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

    result: EEPayrollResult = await compute_ee_payroll(
        gross=gross,
        effective_date=effective_date,
        pillar_ii_rate_percent=employee.ee_pillar_ii_rate_percent,
        basic_exemption_elected=employee.ee_basic_exemption_elected,
        pensionable_age=employee.ee_pensionable_age,
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
    """Run PAYG + super for one employee line. Pure (no writes).

    Dispatches on ``company_jurisdiction`` (Packet 3 — mirrors the
    precedent in ``services.journal._apply_tax_treatment``): every
    EXISTING caller passes the default ``"AU"`` (or omits it), so this
    resolves to the exact same AU branch as before Packet 3 —
    byte-identical AU behaviour. ``"EE"`` is the only other jurisdiction
    wired; anything else still runs the AU branch (matches
    ``Company.jurisdiction``'s own "AU is the only jurisdiction wired
    end-to-end" default note).
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

    wh: WithholdingResult = await compute_withholding(
        session,
        gross_per_period=gross,
        period=period,
        employee=employee,
        effective_date=effective_date,
        medicare_exemption=line_input.medicare_exemption,
    )

    sg: SuperResult = compute_super(
        ote=ote,
        period=period,
        effective_date=effective_date,
    )

    net = _q(gross - wh.payg_amount - deductions_total)

    return ComputedPayLine(
        employee_id=employee.id,
        gross=gross,
        payg=wh.payg_amount,
        super_amount=sg.sg_amount,
        net=net,
        ordinary_hours=Decimal(str(line_input.ordinary_hours)),
        overtime_hours=Decimal(str(line_input.overtime_hours)),
        allowances=[dataclasses.asdict(a) for a in line_input.allowances],
        deductions=[dataclasses.asdict(d) for d in line_input.deductions],
        paid_leave=[dataclasses.asdict(p) for p in line_input.paid_leave],
        lump_sums=[dataclasses.asdict(ls) for ls in line_input.lump_sums],
        ote=ote,
        payg_breakdown=wh.breakdown_note,
        super_breakdown=sg.breakdown_note,
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

    Per-employee shape, per the brief:

        Dr Wages expense       gross
        Dr Super expense       sg
           Cr PAYG WH liability       payg
           Cr Super payable           sg
           Cr Bank (net-pay clearing) net

    A pay run with N employees produces a single JE with N×5 lines
    (or fewer if some employees have $0 super / no PAYG). All lines
    share one ``ref`` to keep the GL legible.
    """
    pay_run = await _get_pay_run(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunV2Error(f"Pay run {pay_run_id} not found")
    if pay_run.status == PayRunStatus.FINALIZED:
        return pay_run
    if pay_run.status not in (PayRunStatus.DRAFT, PayRunStatus.ABA_EXPORTED):
        raise PayRunV2Error(
            f"Cannot finalize from status {pay_run.status!r}"
        )

    # Resolve accounts once.
    co_id = pay_run.company_id

    # EE JE posting is NOT built (Packet 3 scope-narrowing, flagged —
    # not a silent gap): the 5-leg shape below is AU-account-coded
    # (_ACCT_*) and Dr-Wages/Cr-Net-only, which for an EE line would
    # both use the wrong chart-of-accounts convention AND omit the
    # employer-borne legs (social tax, employer unemployment) entirely
    # — Dr gross / Cr net would not even balance, since EE net pay
    # deducts only income tax + employee unemployment + pillar II, not
    # the full gross-minus-net gap the AU shape assumes. Refuse loudly
    # rather than post a broken or AU-shaped JE for an EE company. The
    # EE compute path (``upsert_line``) is unaffected — this guard is
    # finalize-only.
    company_jurisdiction = (
        await session.execute(
            select(Company.jurisdiction).where(Company.id == co_id)
        )
    ).scalar_one_or_none() or "AU"
    if company_jurisdiction != "AU":
        raise PayRunV2Error(
            f"finalize_with_je does not support jurisdiction "
            f"{company_jurisdiction!r} yet — only AU's Dr-Wages/"
            "Dr-Super/Cr-PAYG/Cr-Super/Cr-Net JE shape is implemented. "
            "EE payroll compute (services.payroll_ee via upsert_line) "
            "is available; EE journal posting is a follow-up packet."
        )
    wages = await _account_by_code(session, company_id=co_id, code=_ACCT_WAGES_EXPENSE)
    super_exp = await _account_by_code(session, company_id=co_id, code=_ACCT_SUPER_EXPENSE)
    payg_liab = await _account_by_code(session, company_id=co_id, code=_ACCT_PAYG_LIABILITY)
    super_liab = await _account_by_code(session, company_id=co_id, code=_ACCT_SUPER_LIABILITY)
    net_clearing = await _account_by_code(session, company_id=co_id, code=_ACCT_NET_PAY_CLEARING)

    # Fetch lines.
    lines_result = await session.execute(
        select(PayRunLine).where(PayRunLine.pay_run_id == pay_run_id)
    )
    lines = list(lines_result.scalars().all())
    if not lines:
        raise PayRunV2Error("Pay run has no lines — add at least one before finalize.")

    je_lines: list[dict[str, Any]] = []
    for line in lines:
        emp_label = str(line.employee_id)[:8]
        # Dr Wages
        if line.gross > 0:
            je_lines.append({
                "account_id": wages.id,
                "description": f"Wages: {emp_label}",
                "debit": line.gross,
                "credit": Decimal("0"),
            })
        # Dr Super expense
        if line.super_amount > 0:
            je_lines.append({
                "account_id": super_exp.id,
                "description": f"SG: {emp_label}",
                "debit": line.super_amount,
                "credit": Decimal("0"),
            })
        # Cr PAYG liability
        if line.tax > 0:
            je_lines.append({
                "account_id": payg_liab.id,
                "description": f"PAYG WH: {emp_label}",
                "debit": Decimal("0"),
                "credit": line.tax,
            })
        # Cr Super payable
        if line.super_amount > 0:
            je_lines.append({
                "account_id": super_liab.id,
                "description": f"Super payable: {emp_label}",
                "debit": Decimal("0"),
                "credit": line.super_amount,
            })
        # Cr Net pay clearing
        if line.net > 0:
            je_lines.append({
                "account_id": net_clearing.id,
                "description": f"Net pay: {emp_label}",
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
]
