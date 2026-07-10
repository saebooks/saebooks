"""TSD (income + social + withholding tax return) listing generator —
main-form totals + Lisa 1 per-person rows.

kmd-inf-tsd scope Packet 4 (``~/.claude/plans/kmd-inf-tsd-scope.md``
§1.2/§2.2/§7). Assembles TSD MAIN aggregates + Lisa-1 rows from POSTED
EE pay-run lines (Packet 3's ``services.payroll_ee`` compute, wired
through ``services.pay_runs_v2._compute_ee``).

**Parallel to, not built on, the KMD box engine** (scope §0), same
relationship the KMD-INF generator has: this module reads
``PayRun``/``PayRunLine``/``Employee`` rows directly, not
``tax_return_generator.py``'s 28-box vector.

---

## Design decisions this module had to make where the scope was silent
(flagged prominently per the build's "fix forward minimally and flag
it" instruction):

1. **"Posted" EE pay run — a real engine gap found and fixed forward,
   not silently worked around.** The scope's own premise is TSD
   generation "from posted EE pay runs" (§1.2), i.e. ``PayRun.status
   == FINALIZED`` (the status ``upsert_line`` already treats as the
   write-lock boundary — see its own DRAFT-only guard). But
   ``services.pay_runs_v2.finalize_with_je`` (Packet 3, unmodified)
   hard-refuses any non-AU jurisdiction — its 5-leg JE shape is
   AU-account-coded and EE journal posting was explicitly deferred.
   **There was therefore no way for an EE pay run to ever reach
   FINALIZED** — the scope's own stated TSD data source was
   unreachable. Fixed forward minimally in
   ``services.pay_runs_v2.finalize_ee_status_only``: locks an EE pay
   run's lines (mirrors the DRAFT->FINALIZED transition) WITHOUT
   posting the AU-shaped ledger entry. EE ledger posting remains a
   real, separate, still-open gap — this generator reads
   ``PayRun.status == FINALIZED`` regardless of how that status was
   reached, same as the KMD-INF generator reads ``Invoice.status ==
   POSTED`` regardless of how a document got there. Checked for a
   FINALIZED-implies-journal-posted invariant elsewhere in the tree
   before landing this: ``pay_runs_v2`` (where ``upsert_line``/
   ``finalize_ee_status_only`` live) is not wired to any live API
   route today (grep confirms no ``saebooks/api`` import) — the
   live ``PUT /pay-runs/{id}/finalize`` route uses the separate legacy
   ``services.pay_runs.finalize``, which requires ``journal_id`` be
   set BEFORE allowing FINALIZED and is untouched by this change. The
   only schema that surfaces ``PayRun.journal_id`` (``PayRunOut`` in
   ``api/v1/schemas.py``) already types it ``uuid.UUID | None``. A
   FINALIZED-without-``journal_id`` pay run is therefore safe to
   create today; re-check this note if ``pay_runs_v2`` is ever wired
   to a live route.
2. **Row granularity = one row per posted pay-run line, not one row
   per employee per period.** [FORM-KNOWLEDGE]: Lisa 1 is described as
   a "payment listing" (scope §1.2 "per-resident-person payment
   listing") — read literally as one row per payment event. If a
   company runs more than one EE pay run touching the same employee
   within a calendar month, this emits multiple Lisa-1 rows (never
   silently nets/aggregates payments together) — mirrors the KMD-INF
   generator's "never silently net" posture. **UNVERIFIED** against
   the real TSD täitmise juhend, same caveat class as the rest of this
   scope's form-layout assumptions.
3. **Only EE-computed lines are read.** A pay-run line is included iff
   ``PayRunLine.ee_income_tax IS NOT NULL`` (Packet 3's tell for "this
   line went through the EE compute path", mirrors
   ``test_pay_runs_v2_ee.py``'s own assertion that AU lines leave the
   EE columns NULL). Belt-and-braces alongside the company-jurisdiction
   filter — a line with EE columns NULL never becomes a Lisa-1 row
   even if somehow selected.
4. **Missing isikukood — data-quality error, not silent drop.**
   Mirrors KMD-INF's "partner with no code but >=EUR1,000 is a
   data-quality error to surface" (scope §2.1) exactly: an EE-computed
   pay-run line whose employee has no ``isikukood_encrypted`` on file
   cannot be a Lisa-1 row (isikukood *is* the row key, scope §3.2) —
   surfaced in ``TsdListing.errors``, excluded from ``lisa1``, never
   silently dropped or emitted with a blank key.
5. **``basic_exemption_applied`` is re-derived, not read off
   ``PayRunLine``.** Packet 3's ``ComputedPayLine``/``PayRunLine``
   never persisted ``EEPayrollResult.basic_exemption_applied`` (a real
   gap in Packet 3, found here) — only the four withheld/contributed
   AMOUNTS were given columns. Rather than add a sixth EE column (a
   third company-DB migration this packet was not scoped to add), this
   module re-derives the exemption amount by calling
   ``compute_ee_payroll`` again with the SAME inputs
   ``upsert_line``/``_compute_ee`` used
   (``gross=line.gross``, ``effective_date=pay_run.payment_date``,
   the employee's election flags) — a pure, deterministic function of
   those inputs, so it reproduces the original figure exactly *unless*
   the reference-DB rate rows changed between pay-run compute time and
   TSD generation time (not a concern for a same-month posted run;
   flagged here rather than assumed away). The re-derived income tax
   is checked against the persisted ``PayRunLine.ee_income_tax`` — a
   mismatch means the rates moved, and (consistent with every other
   bad-line case in this module, point 4 above) becomes a
   ``TsdDataQualityError`` for that one line rather than aborting the
   whole company's listing.
6. **Payment-type code is a single PLACEHOLDER constant.** Board-member
   fees are out of scope (``services.payroll_ee`` module docstring,
   scope §2.2's own flag: "NOT modelled here") — every line this
   module reads is ordinary gross-wage payroll, so every Lisa-1 row
   carries the same payment-type classification. The real EMTA
   väljamakse liik CODE (an actual number on the wire) is
   **UNVERIFIED** — unlike KMD-INF's erisuse-kood values (scope-cited
   from [SEED-EE]), no source in this tree names the real ordinary-wage
   code, so this module does not guess a numeric value; it exports a
   named, obviously-not-final placeholder
   (``PAYMENT_TYPE_WAGES``) for the future ``tsd/mapping.py`` (Packet
   5) to translate into the real wire code once sourced.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.employee import Employee
from saebooks.models.pay_run import PayRun, PayRunLine, PayRunStatus
from saebooks.services.crypto import FieldDecryptionError
from saebooks.services.employees import decrypt_isikukood
from saebooks.services.payroll_ee import EERates, compute_ee_payroll, resolve_ee_rates

_ZERO = Decimal("0")

# ⚠ UNVERIFIED (module docstring point 6) — not a real EMTA väljamakse
# liik code, a named placeholder pending the real value + Packet 5's
# tsd/mapping.py wire-name/code translation.
PAYMENT_TYPE_WAGES = "PLACEHOLDER_PAYMENT_TYPE_WAGES"


@dataclass(frozen=True)
class TsdLisa1Row:
    """One Lisa-1 row — one posted EE pay-run line (module docstring
    point 2: one row per payment, not per employee per period)."""

    employee_id: uuid.UUID
    isikukood: str
    payment_type_code: str
    gross: Decimal
    basic_exemption_applied: Decimal
    income_tax: Decimal
    unemployment_employee: Decimal
    pillar_ii: Decimal
    social_tax: Decimal
    unemployment_employer: Decimal
    pay_run_id: uuid.UUID
    payment_date: date


@dataclass(frozen=True)
class TsdMainTotals:
    """TSD MAIN form aggregate totals — "a trivial roll-up" of Lisa 1
    once Lisa 1 exists (scope §2.2)."""

    employee_count: int
    total_gross: Decimal
    total_income_tax: Decimal
    total_unemployment_employee: Decimal
    total_unemployment_employer: Decimal
    total_social_tax: Decimal
    total_pillar_ii: Decimal


@dataclass(frozen=True)
class TsdDataQualityError:
    """A posted EE pay-run line has no isikukood on file for its
    employee — scope's own "data-quality error to surface, not a
    silent drop" posture (mirrors KMD-INF's no-registration-number
    case, scope §2.1), applied to TSD's row key (module docstring
    point 4)."""

    employee_id: uuid.UUID
    employee_name: str
    pay_run_id: uuid.UUID
    message: str


@dataclass(frozen=True)
class TsdListing:
    company_id: uuid.UUID
    period_start: date
    period_end: date
    main: TsdMainTotals
    lisa1: list[TsdLisa1Row] = field(default_factory=list)
    errors: list[TsdDataQualityError] = field(default_factory=list)
    # Critic round 2 finding: every EE pay run reaching FINALIZED today
    # does so via ``pay_runs_v2.finalize_ee_status_only`` (module
    # docstring point 1), which locks lines WITHOUT posting a journal
    # entry — ``journal_id`` stays NULL. So TSD totals are sourced from
    # pay runs with NO corresponding GL postings (no wages expense, no
    # PAYE/social-tax/pillar-II liability, no net-pay-payable) — the
    # trial balance silently omits real payroll liabilities until EE
    # ledger posting is built (a separate, larger, already-flagged gap
    # — see this module's + ``pay_runs_v2.finalize_ee_status_only``'s
    # docstrings; NOT closed by this field). Kept OUT of ``errors``
    # (that list is per-LINE data-quality problems the row-exclusion
    # logic above acts on; this is a listing-wide provenance fact, true
    # of every row today) so a caller can surface "these totals have no
    # GL backing yet" without it being mistaken for an excluded/bad row.
    gl_not_posted_pay_run_ids: list[uuid.UUID] = field(default_factory=list)


def _main_totals(rows: list[TsdLisa1Row]) -> TsdMainTotals:
    return TsdMainTotals(
        employee_count=len({r.employee_id for r in rows}),
        total_gross=sum((r.gross for r in rows), _ZERO),
        total_income_tax=sum((r.income_tax for r in rows), _ZERO),
        total_unemployment_employee=sum((r.unemployment_employee for r in rows), _ZERO),
        total_unemployment_employer=sum((r.unemployment_employer for r in rows), _ZERO),
        total_social_tax=sum((r.social_tax for r in rows), _ZERO),
        total_pillar_ii=sum((r.pillar_ii for r in rows), _ZERO),
    )


async def generate_tsd(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> TsdListing:
    """Assemble the TSD MAIN totals + Lisa-1 row set for one period.

    Period basis is ``PayRun.payment_date`` (module docstring point 1
    context: TSD reports amounts withheld from payments MADE in the
    period — the same field ``_compute_ee`` already uses as its
    ``effective_date``, so period selection and rate resolution never
    drift against each other).
    """
    pay_run_result = await session.execute(
        select(PayRun).where(
            PayRun.company_id == company_id,
            PayRun.status == PayRunStatus.FINALIZED,
            PayRun.payment_date >= period_start,
            PayRun.payment_date <= period_end,
            PayRun.archived_at.is_(None),
        ).order_by(PayRun.payment_date)
    )
    pay_runs = list(pay_run_result.scalars().all())
    if not pay_runs:
        return TsdListing(
            company_id=company_id, period_start=period_start, period_end=period_end,
            main=_main_totals([]),
        )
    pay_run_by_id = {pr.id: pr for pr in pay_runs}

    line_result = await session.execute(
        select(PayRunLine).where(
            PayRunLine.pay_run_id.in_(pay_run_by_id.keys()),
            PayRunLine.ee_income_tax.is_not(None),
        )
    )
    lines = list(line_result.scalars().all())
    if not lines:
        return TsdListing(
            company_id=company_id, period_start=period_start, period_end=period_end,
            main=_main_totals([]),
        )

    employee_ids = {ln.employee_id for ln in lines}
    emp_result = await session.execute(
        select(Employee).where(Employee.id.in_(employee_ids))
    )
    employees = {e.id: e for e in emp_result.scalars().all()}

    # Deterministic order: employee_number, then payment_date — avoids
    # the same flakiness class fixed forward in the KMD-INF generator.
    def _sort_key(ln: PayRunLine) -> tuple[str, date]:
        emp = employees.get(ln.employee_id)
        number = emp.employee_number if emp is not None else ""
        pr = pay_run_by_id[ln.pay_run_id]
        return (number, pr.payment_date)

    rows: list[TsdLisa1Row] = []
    errors: list[TsdDataQualityError] = []

    # Critic round 1 fix: rows commonly share ``pay_run.payment_date``
    # (one payment run per month) — resolve the reference-DB rate rows
    # ONCE per distinct effective_date and reuse across every row for
    # that date, instead of a fresh ``ReferenceSession`` + 3 queries per
    # row (was an uncached N+1 across a separate connection pool).
    rates_cache: dict[date, EERates] = {}

    for ln in sorted(lines, key=_sort_key):
        pay_run = pay_run_by_id[ln.pay_run_id]
        employee = employees.get(ln.employee_id)
        if employee is None:
            # FK is RESTRICT — shouldn't happen for a posted line —
            # degrade to a data-quality error rather than raise.
            errors.append(TsdDataQualityError(
                employee_id=ln.employee_id, employee_name="Unknown",
                pay_run_id=ln.pay_run_id,
                message=f"Employee {ln.employee_id} not found for a posted EE pay-run line.",
            ))
            continue

        # Critic round 3 fix: decrypt_isikukood -> services.crypto.decrypt_field
        # raises FieldDecryptionError (InvalidToken) on a corrupt/wrong-key
        # ciphertext. Every other bad-line case in this module degrades to
        # a TsdDataQualityError so one bad row never aborts the whole
        # company's TSD listing (module docstring point 4) — an unguarded
        # decrypt here would have violated that same contract by letting
        # one employee's corrupt isikukood abort the entire period.
        try:
            isikukood = decrypt_isikukood(employee)
        except FieldDecryptionError:
            errors.append(TsdDataQualityError(
                employee_id=employee.id, employee_name=employee.employee_number,
                pay_run_id=ln.pay_run_id,
                message=(
                    f"Employee {employee.employee_number}'s isikukood_encrypted "
                    "could not be decrypted (wrong key or corrupt ciphertext) — "
                    "cannot be listed as a TSD Lisa-1 row."
                ),
            ))
            continue
        if not isikukood:
            errors.append(TsdDataQualityError(
                employee_id=employee.id, employee_name=employee.employee_number,
                pay_run_id=ln.pay_run_id,
                message=(
                    f"Employee {employee.employee_number} has a posted EE pay-run "
                    "line but no isikukood on file — cannot be listed as a TSD "
                    "Lisa-1 row. Set Employee.isikukood_encrypted to resolve."
                ),
            ))
            continue

        # Re-derive the one Lisa-1 field Packet 3 never persisted
        # (module docstring point 5), and integrity-check the
        # persisted amounts against a fresh compute of the same inputs.
        rates = rates_cache.get(pay_run.payment_date)
        if rates is None:
            rates = await resolve_ee_rates(pay_run.payment_date)
            rates_cache[pay_run.payment_date] = rates
        result = await compute_ee_payroll(
            gross=ln.gross,
            effective_date=pay_run.payment_date,
            pillar_ii_rate_percent=employee.ee_pillar_ii_rate_percent,
            basic_exemption_elected=employee.ee_basic_exemption_elected,
            pensionable_age=employee.ee_pensionable_age,
            rates=rates,
        )
        if result.income_tax != ln.ee_income_tax:
            # Surfaced, not raised — every other bad-line case in this
            # module degrades to a TsdDataQualityError so one stale
            # line never aborts the whole company's TSD listing; a
            # rate-table drift is no different (module docstring point
            # 5 update: was a hard raise, corrected to match the rest
            # of the module's "surface, don't crash" posture).
            errors.append(TsdDataQualityError(
                employee_id=employee.id, employee_name=employee.employee_number,
                pay_run_id=ln.pay_run_id,
                message=(
                    f"Re-derived income tax ({result.income_tax}) disagrees "
                    f"with the persisted pay-run-line amount "
                    f"({ln.ee_income_tax}) for employee "
                    f"{employee.employee_number}, pay run {ln.pay_run_id} — "
                    "the reference-DB EE rate rows likely changed between "
                    "pay-run compute time and TSD generation time. Row "
                    "excluded pending investigation."
                ),
            ))
            continue

        rows.append(TsdLisa1Row(
            employee_id=employee.id,
            isikukood=isikukood,
            payment_type_code=PAYMENT_TYPE_WAGES,
            gross=ln.gross,
            basic_exemption_applied=result.basic_exemption_applied,
            income_tax=ln.ee_income_tax,
            unemployment_employee=ln.ee_unemployment_employee,
            pillar_ii=ln.ee_pillar_ii,
            social_tax=ln.ee_social_tax,
            unemployment_employer=ln.ee_unemployment_employer,
            pay_run_id=ln.pay_run_id,
            payment_date=pay_run.payment_date,
        ))

    # Critic round 2 finding — see TsdListing.gl_not_posted_pay_run_ids'
    # own docstring: every source pay run that contributed at least one
    # EE-computed line, with no journal entry posted for it.
    gl_not_posted_ids = sorted(
        {
            pay_run_by_id[ln.pay_run_id].id
            for ln in lines
            if pay_run_by_id[ln.pay_run_id].journal_id is None
        },
        key=str,
    )

    return TsdListing(
        company_id=company_id, period_start=period_start, period_end=period_end,
        main=_main_totals(rows), lisa1=rows, errors=errors,
        gl_not_posted_pay_run_ids=gl_not_posted_ids,
    )


__all__ = [
    "PAYMENT_TYPE_WAGES",
    "TsdDataQualityError",
    "TsdLisa1Row",
    "TsdListing",
    "TsdMainTotals",
    "generate_tsd",
]
