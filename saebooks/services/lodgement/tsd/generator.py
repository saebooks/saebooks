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

from sqlalchemy import func, select
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


# =============================================================================
# Module 1 (kmd-inf-tsd-scope / ee-frontier-build-plan.md §"MODULE 1") — Lisa
# 2-7 row/aggregate dataclasses + generator stubs.
#
# SCOPE BOUNDARY (build-plan §1.4, restated so it is not lost on read):
# these dataclasses + the pure totals-roll-up helpers below are buildable now
# (no new EE source-data model needed — they are typed containers + arithmetic
# over rows a caller already has). The `generate_tsd_lisaN(session, ...)`
# ASSEMBLY functions that would read the engine's own tables and populate
# those rows are NOT buildable yet — each raises ``NotImplementedError``
# naming the missing EE source-data model, mirroring
# ``services.lodgement.remote.py``'s ``poll_status`` gated-stub discipline
# (module docstring point 1's own precedent, applied to a whole annex instead
# of one route). See ``tsd/serializer.py`` for what IS shippable: the
# XML/CSV rendering of these row types is complete and XSD-validated.
# =============================================================================


# ---- Lisa 2 (non-resident payments/withholding) ---------------------------
# Populated in the official example (tsd_L2_A/B_Isik/Vm/Mvt +
# tsd_L2_2_Inv_Fond/Vm) — see mapping.py's module docstring for element
# names. Row shape mirrors TsdLisa1Row's own choice: ONE FLAT ROW per
# person-payment (or fund-payment), grouped by key in the serializer
# (mirrors ``_group_by_person`` above) — not a pre-grouped tree, so a future
# generator can emit rows the same way ``generate_tsd`` does today.

@dataclass(frozen=True)
class TsdLisa2MvtRow:
    """One ``tsd_L2_A_Mvt`` / ``tsd_L2_B_Mvt`` child — an income-type
    breakdown line nested under one A/B payment row."""

    source_code: str    # c2154_TuliKood / c2454_TuliKood
    amount: Decimal      # c2155_Summa / c2455_Summa


@dataclass(frozen=True)
class TsdLisa2ARow:
    """One row per resident-country A-subform payment to a non-resident
    natural person (``tsd_L2_A_Isik`` -> ``tsd_L2_A_Vm``, XSD ``tsdL2AVm``).
    ``mvt`` is that one payment's own income-type breakdown (``mvtList``)."""

    isikukood: str                          # c2000_Kood
    name: str | None                        # c2010_Nimi
    country_code: str | None                # c2020_RiikKood
    payment_type_code: str                  # c2030_ValiKood
    gross: Decimal                          # c2040_Summa
    a1_certificate_country_code: str | None  # c2060_RiikKood
    social_tax_base: Decimal | None         # c2070_Smvm
    incapacity_pension_deducted: Decimal | None    # c2080_TvpVah
    prior_month_rate_deducted: Decimal | None      # c2090_KuumVah
    minimum_social_tax_increase: Decimal | None    # c2100_KuumSuur
    social_tax: Decimal | None              # c2110_Sm
    unemployment_base: Decimal | None       # c2120_Tkvm
    unemployment_employee: Decimal | None   # c2130_Tk
    unemployment_employer: Decimal | None   # c2140_Ttk
    income_tax_base: Decimal | None         # c2150_Tmvm
    income_tax_rate: Decimal | None         # c2160_TmMaar
    income_tax: Decimal | None              # c2170_Tm
    mvt: tuple[TsdLisa2MvtRow, ...] = ()


@dataclass(frozen=True)
class TsdLisa2BRow:
    """One row per B-subform payment (return/offset of a prior
    distribution) to a non-resident natural person (``tsd_L2_B_Isik`` ->
    ``tsd_L2_B_Vm``, XSD ``tsdL2BVm``)."""

    isikukood: str                          # c2300_Kood
    name: str | None                        # c2310_Nimi
    payment_type_code: str                  # c2320_ValiKood
    gross: Decimal                          # c2330_Summa
    year: int | None                        # c2340_Aasta
    month: int | None                       # c2350_Kuu
    reason_code: str | None                 # c2360_Pohjus
    social_tax_base: Decimal | None         # c2370_Smvm
    social_tax_base_deducted: Decimal | None       # c2380_SmvmVah
    social_tax_base_increase: Decimal | None       # c2390_SmvmSuur
    social_tax_base_adjustment: Decimal | None     # c2400_SmvmSk
    social_tax: Decimal | None              # c2410_Sm
    unemployment_base: Decimal | None       # c2420_Tkvm
    unemployment_employee: Decimal | None   # c2430_Tk
    unemployment_employer: Decimal | None   # c2440_Ttk
    income_tax_base: Decimal | None         # c2450_Tmvm
    income_tax_rate: Decimal | None         # c2460_TmMaar
    income_tax: Decimal | None              # c2470_Tm
    reason_explanation: str | None = None   # pohjusSelgitus
    mvt: tuple[TsdLisa2MvtRow, ...] = ()


@dataclass(frozen=True)
class TsdLisa2InvFondRow:
    """One row per payment listed under a contractual investment fund
    (``tsd_L2_2_Inv_Fond`` -> ``tsd_L2_2_Vm``, XSD ``tsdL22InvFond``/
    ``tsdL22Vm``). Fund header fields are repeated per payment row, same
    flat-row-grouped-in-serializer discipline as the A/B rows above."""

    fund_code: str                          # c2700_Kood
    fund_name: str | None                   # c2710_Nimi
    fund_country_code: str | None           # c2720_RiikKood
    manager_code: str | None                # c2730_FvKood
    manager_name: str | None                # c2740_FvNimi
    manager_country_code: str | None        # c2750_FvRiikKood
    participation_percent: Decimal          # c2780_Osalus
    payment_type_code: str                  # c2760_ValiKood
    amount: Decimal                         # c2770_Summa
    income_tax: Decimal | None              # c2790_Tm


@dataclass(frozen=True)
class TsdLisa2Totals:
    """Annex-level roll-up totals (all "Calculated" in the XSD — e-MTA
    derives them from the row data; ``compute_lisa2_totals`` below
    reproduces the LITERAL-SUM ones the XSD documentation states in
    plain language, e.g. "Calculated: ... in total for 1a"). The
    "entire 3 years" secondary series (c2201/c2211/c2221/c2231/c2241/
    c2251) is NOT reproduced here — its derivation depends on a
    multi-year lookback the row model does not carry; omitted (XSD
    ``minOccurs=0``, so a listing with only the primary totals still
    validates). ⚠ UNVERIFIED beyond "these are sums of the rows below
    them" (not independently confirmed against a second worked
    example)."""

    social_tax_base_a: Decimal              # c2200_Smvm
    social_tax_a: Decimal                   # c2210_Sm
    unemployment_employee_a: Decimal        # c2220_Tk
    unemployment_employer_a: Decimal        # c2230_Ttk
    income_tax_base_a: Decimal              # c2240_Tmvm
    income_tax_a: Decimal                   # c2250_Tm
    social_tax_base_b: Decimal              # c2500_Smvm
    social_tax_b: Decimal                   # c2510_Sm
    unemployment_employee_b: Decimal        # c2520_Tk
    unemployment_employer_b: Decimal        # c2530_Ttk
    income_tax_base_b: Decimal              # c2540_Tmvm
    income_tax_b: Decimal                   # c2550_Tm
    inv_fond_income_tax: Decimal            # c2800_InvTm


def compute_lisa2_totals(
    a_rows: list[TsdLisa2ARow],
    b_rows: list[TsdLisa2BRow],
    inv_fond_rows: list[TsdLisa2InvFondRow],
) -> TsdLisa2Totals:
    """Pure roll-up over already-assembled rows — no DB access, so
    buildable without the non-resident-payee source model (unlike
    ``generate_tsd_lisa2`` below, which needs that model to produce the
    rows themselves)."""
    return TsdLisa2Totals(
        social_tax_base_a=sum((r.social_tax_base or _ZERO for r in a_rows), _ZERO),
        social_tax_a=sum((r.social_tax or _ZERO for r in a_rows), _ZERO),
        unemployment_employee_a=sum((r.unemployment_employee or _ZERO for r in a_rows), _ZERO),
        unemployment_employer_a=sum((r.unemployment_employer or _ZERO for r in a_rows), _ZERO),
        income_tax_base_a=sum((r.income_tax_base or _ZERO for r in a_rows), _ZERO),
        income_tax_a=sum((r.income_tax or _ZERO for r in a_rows), _ZERO),
        social_tax_base_b=sum((r.social_tax_base or _ZERO for r in b_rows), _ZERO),
        social_tax_b=sum((r.social_tax or _ZERO for r in b_rows), _ZERO),
        unemployment_employee_b=sum((r.unemployment_employee or _ZERO for r in b_rows), _ZERO),
        unemployment_employer_b=sum((r.unemployment_employer or _ZERO for r in b_rows), _ZERO),
        income_tax_base_b=sum((r.income_tax_base or _ZERO for r in b_rows), _ZERO),
        income_tax_b=sum((r.income_tax or _ZERO for r in b_rows), _ZERO),
        inv_fond_income_tax=sum((r.income_tax or _ZERO for r in inv_fond_rows), _ZERO),
    )


@dataclass(frozen=True)
class TsdLisa2Listing:
    a_rows: list[TsdLisa2ARow] = field(default_factory=list)
    b_rows: list[TsdLisa2BRow] = field(default_factory=list)
    inv_fond_rows: list[TsdLisa2InvFondRow] = field(default_factory=list)
    totals: TsdLisa2Totals | None = None


async def generate_tsd_lisa2(
    session: AsyncSession, *, company_id: uuid.UUID, period_start: date, period_end: date,
) -> TsdLisa2Listing:
    """**BLOCKED** — Lisa 2 lists payments to NON-RESIDENT payees
    (natural persons + contractual investment funds). The engine has no
    non-resident-payee source model: ``Employee`` carries no
    residency/country flag and no non-resident-specific väljamakse-liik
    classification (build-plan §1.4). Prerequisite: a non-resident payee
    record (residency flag + country code + non-resident väljamakse-liik)
    before this can read real rows the way ``generate_tsd`` reads
    ``PayRunLine``. The row/totals dataclasses and the XML/CSV
    serialization above ARE shippable now — only this assembly step is
    gated."""
    raise NotImplementedError(
        "TSD Lisa 2 generator — blocked on a non-resident-payee source "
        "model (residency flag + country + non-resident väljamakse-liik "
        "classification). No such model exists on Employee or elsewhere "
        "in the engine today. See ee-frontier-build-plan.md §1.4."
    )


# ---- Lisa 3 (special: permanent establishment / CFC / disguised profit) ---
# ABSENT from the official example entirely (build-plan §0.3) — the
# weakest-golden annex. XSD ``tsdL30`` is `xs:all`, every element
# ``minOccurs="0"``, so an ALL-EMPTY ``tsd_L3_0`` is itself XSD-valid; the
# fields below are the header scalars (``tsd_L3_2_Tomava``/``tsd_L3_4_
# Andmine``/``tsd_L3_4_Saamine``/``tsd_L3_6`` repeating lists are NOT
# modelled — no worked example exists anywhere in the package to pin their
# real-world shape against, so building them now would be pure guesswork
# beyond what the XSD element names alone justify. Header-only, hand-
# authored, XSD-validated ONLY — see mapping.py's docstring for the same
# caveat repeated at the point of use).

@dataclass(frozen=True)
class TsdLisa3Header:
    """Lisa 3 main-block header scalars only (``tsdL30``'s ``c30xx``/
    ``c32xx``/``c33xx`` fields) — all ``minOccurs=0`` in the XSD, all
    optional here too. The three repeating sub-lists
    (Tomava/Andmine/Saamine) and ``tsd_L3_6`` are OUT OF SCOPE (see module
    section docstring above)."""

    profit_removed_from_pe: Decimal | None = None       # c3000_VKasum
    profit_treaty_exempt: Decimal | None = None          # c3010_Mv
    treaty_country_code: str | None = None                # c3020_RiikKood
    assets_imported: Decimal | None = None                # c3333_ToodudVara
    exempt_income_total: Decimal | None = None            # c3200_VKokku
    deductible_tax_total: Decimal | None = None           # c3210_MKokku
    exempt_income_opening: Decimal | None = None          # c3220_VAlgjaak
    exempt_income_available: Decimal | None = None        # c3230_VabaV
    deductible_tax_opening: Decimal | None = None         # c3240_MAlgjaak
    deductible_tax_available: Decimal | None = None       # c3250_VabaM
    pe_profit_exempt: Decimal | None = None                # c3260_MvKasumV
    pe_taxable_profit: Decimal | None = None               # c3270_MKasum
    pe_income_tax: Decimal | None = None                   # c3280_Tm
    income_tax_reducing_liability: Decimal | None = None  # c3290_MaTmM
    special_income_tax_payable: Decimal | None = None     # c3300_TmEj
    exempt_income_closing: Decimal | None = None           # c3310_VJaak
    deductible_tax_closing: Decimal | None = None          # c3320_MJaak
    annex3_income_tax: Decimal | None = None                # c3350_Tm
    loan_disguised_as_distribution: Decimal | None = None  # c3810_AntudLaen
    cfc_profit: Decimal | None = None                       # c3815_AyTulu
    cfc_income_tax: Decimal | None = None                   # c3820_TmKe


async def generate_tsd_lisa3(
    session: AsyncSession, *, company_id: uuid.UUID, period_start: date, period_end: date,
) -> TsdLisa3Header:
    """**BLOCKED** — Lisa 3 reports permanent-establishment profit
    removal, controlled-foreign-company (CFC) profit, and disguised
    profit distributions (loans to related parties economically
    equivalent to a distribution). The engine holds no PE/CFC/disguised-
    distribution source model — no foreign-branch ledger tagging, no CFC
    ownership register, no related-party-loan classification. Prerequisite:
    those three source models before this annex can be honestly
    generated."""
    raise NotImplementedError(
        "TSD Lisa 3 generator — blocked on a permanent-establishment / "
        "controlled-foreign-company / disguised-distribution source model. "
        "None of the three exists in the engine today. See "
        "ee-frontier-build-plan.md §1.4 (L3 prerequisite not itemised "
        "there — named here instead: PE ledger tag, CFC ownership "
        "register, related-party-loan classification)."
    )


# ---- Lisa 4 (fringe benefits / erisoodustused) -----------------------------
# Header-only in the official example (totals populated, no repeating rows
# in the XSD for L4 at all — ``tsdL40`` has no list children).

@dataclass(frozen=True)
class TsdLisa4Header:
    """Lisa 4 main-block header scalars (``tsdL40``'s ``c40xx``/``c41xx``
    fields) — semi-strong golden: the official example populates every
    one of these (build-plan §0.3), just with no repeating rows (there are
    none in the XSD for this annex)."""

    electricity_expense: Decimal | None = None             # c4000_ElKulu
    fuel_expense: Decimal | None = None                     # c4010_KiKulu
    housing_benefit: Decimal | None = None                  # c4030_Is
    transport_benefit: Decimal | None = None                # c4040_Ts
    other_benefit: Decimal | None = None                    # c4050_Mv
    below_market_loan: Decimal | None = None                # c4060_SoLaen
    market_interest_rate: Decimal | None = None             # c4061_TuruIntr
    loan_interest_rate: Decimal | None = None                # c4062_LaenIntr
    below_cost_transfer: Decimal | None = None                # c4070_AllaTh
    market_value: Decimal | None = None                       # c4071_Th
    sale_price: Decimal | None = None                          # c4072_Rh
    above_cost_acquisition: Decimal | None = None             # c4080_OoTulu
    acquisition_market_value: Decimal | None = None            # c4081_OoTh
    acquisition_price: Decimal | None = None                    # c4082_ORh
    acquisition_percent: Decimal | None = None                  # c4083_Op
    above_market_sale: Decimal | None = None                    # c4090_YleTh
    sale_market_value: Decimal | None = None                    # c4091_Rh
    sale_actual_price: Decimal | None = None                    # c4092_Th
    waived_claim: Decimal | None = None                          # c4100_LoobuRn
    business_entertainment_expense: Decimal | None = None      # c4110_KoKulu
    other_fringe_expense: Decimal | None = None                  # c4120_TeKulu
    special_benefit_expenses: Decimal | None = None              # c4130_MEs
    total_expenses_incl_vat: Decimal | None = None                # c4140_EsSumma
    prior_period_income_tax: Decimal | None = None                # c4150_EiTm
    prior_period_social_tax: Decimal | None = None                 # c4160_EiSm
    special_income_tax: Decimal | None = None                       # c4170_TmEj
    social_tax: Decimal | None = None                                # c4180_Sm
    social_tax_on_expenses: Decimal | None = None                    # c4181_SmEs


_LISA4_CATEGORY_FIELD: dict[str, str] = {
    "motor_vehicle": "transport_benefit",  # c4040_Ts
    "housing": "housing_benefit",          # c4030_Is
}
_LISA4_DEFAULT_FIELD = "other_benefit"     # c4050_Mv — every other category


async def generate_tsd_lisa4(
    session: AsyncSession, *, company_id: uuid.UUID, period_start: date, period_end: date,
) -> TsdLisa4Header:
    """UNBLOCKED (kmd-inf-tsd follow-up, Packet 2) — the prerequisite
    named in the original block comment (an EE erisoodustus event model)
    now exists: ``services.fringe_benefits_ee`` computes it,
    ``services.pay_runs_v2._compute_ee`` attaches it to a pay-run line
    (``PayRunLine.ee_fringe_benefits`` / ``.ee_fringe_benefit_income_tax``
    / ``.ee_fringe_benefit_social_tax``, ``0197_ee_fringe_benefit_cols``).
    Sources from FINALIZED EE pay runs the same way ``generate_tsd``
    does for Lisa 1 (period basis = ``PayRun.payment_date``,
    ``archived_at IS NULL``) — but filters on fringe-benefit presence
    (``ee_fringe_benefits`` non-empty), NOT ``ee_income_tax IS NOT
    NULL`` (Lisa 1's tell): a line can carry a fringe benefit with no
    wage withholding on it and vice versa — the two are independent EE
    tax events (``services.fringe_benefits_ee`` module docstring).

    Field mapping — only what is CONFIRMED, not guessed (per this
    build's "stub loudly, never fake it" instruction):

    * ``c4040_Ts`` (``transport_benefit``) — sum of every
      ``benefit_category == "motor_vehicle"`` entry's taxable value.
      Confident: "Ts" (transpordivahend/transport) is the only category
      among the 6 c40xx "kulud" fields that plausibly houses a company
      car, and it is the ONLY category this packet's compute produces
      for the car case.
    * ``c4030_Is`` (``housing_benefit``) — sum of ``"housing"`` category
      entries, if any (this packet's compute supports the generic
      cash-value shape for any category string; "housing" is the one
      other category with an unambiguous XSD field).
    * ``c4050_Mv`` (``other_benefit``) — sum of every other category
      (anything not "motor_vehicle" or "housing").
    * ``c4140_EsSumma`` (``total_expenses_incl_vat``) — sum of ALL
      taxable values across every category (confirmed by the "kokku"/
      total-row shape of the c4140 field name and its presence in the
      "semi-strong golden" official example — build-plan §0.3).
    * ``c4180_Sm`` (``social_tax``) — sum of ALL ``social_tax`` across
      every entry. Test-backed (``test_tsd_lisa4_serializer.py`` already
      exercises this exact field with a populated value from the
      official example) and the ONLY current-period tax total this
      header format is confirmed to carry.

    **NOT populated — flagged, not guessed:** the current-period INCOME
    TAX total (this compute produces ``income_tax`` per benefit
    alongside ``social_tax``) has NO confirmed home on this header. The
    two candidates in the XSD-derived field list
    (``prior_period_income_tax``/``c4150_EiTm`` and
    ``special_income_tax``/``c4170_TmEj``) are both named for something
    OTHER than "this period's ordinary fringe-benefit income tax" on
    their face ("prior period" / "special"), and the official example
    fixture this tree carries (``test_tsd_lisa4_serializer.py``'s
    ``_header()``) does not populate either one, so there is no
    arithmetic to check a guess against (unlike ``c4180_Sm``, which IS
    test-backed). Rather than pin ``c4170_TmEj`` by etymology alone
    (Estonian "Tm" = tulumaks is solid; "Ej" is NOT confidently
    "Erisoodustuselt" vs "erijuhtudel" without a source), this ships
    WITHOUT that field populated. The income-tax total is still fully
    computed and available per-line
    (``PayRunLine.ee_fringe_benefit_income_tax``) and per-pay-run (GL:
    ``_finalize_ee``'s fringe-benefit income-tax-payable leg) — only
    ITS SLOT ON THIS SPECIFIC XSD FORM is the open item. Confirm the
    real c4150/c4170 semantics against e-MTA's TSD täitmise juhend (or a
    second populated official example that includes them) before
    wiring it in.
    """
    pay_run_result = await session.execute(
        select(PayRun).where(
            PayRun.company_id == company_id,
            PayRun.status == PayRunStatus.FINALIZED,
            PayRun.payment_date >= period_start,
            PayRun.payment_date <= period_end,
            PayRun.archived_at.is_(None),
        )
    )
    pay_run_ids = [pr.id for pr in pay_run_result.scalars().all()]
    if not pay_run_ids:
        return TsdLisa4Header()

    line_result = await session.execute(
        select(PayRunLine).where(
            PayRunLine.pay_run_id.in_(pay_run_ids),
            func.jsonb_array_length(PayRunLine.ee_fringe_benefits) > 0,
        )
    )
    lines = list(line_result.scalars().all())
    if not lines:
        return TsdLisa4Header()

    totals_by_field: dict[str, Decimal] = {}
    total_expenses = _ZERO
    total_social_tax = _ZERO
    for ln in lines:
        for benefit in ln.ee_fringe_benefits or []:
            value = Decimal(str(benefit.get("taxable_value", "0")))
            social_tax = Decimal(str(benefit.get("social_tax", "0")))
            field_name = _LISA4_CATEGORY_FIELD.get(
                benefit.get("benefit_category", ""), _LISA4_DEFAULT_FIELD
            )
            totals_by_field[field_name] = totals_by_field.get(field_name, _ZERO) + value
            total_expenses += value
            total_social_tax += social_tax

    return TsdLisa4Header(
        transport_benefit=totals_by_field.get("transport_benefit"),
        housing_benefit=totals_by_field.get("housing_benefit"),
        other_benefit=totals_by_field.get("other_benefit"),
        total_expenses_incl_vat=total_expenses if total_expenses else None,
        social_tax=total_social_tax if total_social_tax else None,
    )


# ---- Lisa 5 (gifts / donations / entertainment) ----------------------------
# Header-only in the official example (totals populated); one repeating
# list (tsd_L5_3) NOT populated in the example — out of scope here for the
# same reason as Lisa 3's repeating lists (no worked example to pin
# against); the header alone is still a real, XSD-valid, semi-strong
# golden.

@dataclass(frozen=True)
class TsdLisa5Header:
    """Lisa 5 main-block header scalars (``tsdL50``'s ``c50xx``/``c52xx``
    fields)."""

    gifts_total: Decimal | None = None                      # c5000_Ki
    prior_gift_month: Decimal | None = None                  # c5010_IKiKuu (int per XSD? see mapping.py)
    prior_gift_year: Decimal | None = None                    # c5020_IKiAasta
    deductible_amount: Decimal | None = None                   # c5040_IKasSumma
    ten_percent_cap: Decimal | None = None                      # c5050_10Prots
    taxable_gifts: Decimal | None = None                         # c5060_IMs
    gift_income_tax: Decimal | None = None                        # c5070_ITm
    gift_income_tax_paid: Decimal | None = None                    # c5080_ITasTm
    gift_income_tax_refunded: Decimal | None = None                 # c5090_ITagTm
    prior_business_gift_month: Decimal | None = None                 # c5100_KyKuluKuu
    prior_business_gift_year: Decimal | None = None                   # c5110_KyKuluAasta
    business_gift_base: Decimal | None = None                          # c5120_KyIsmv
    business_gift_income_tax: Decimal | None = None                    # c5130_KyTm
    business_gift_income_tax_paid: Decimal | None = None               # c5140_KyTasTm
    business_gift_income_tax_refunded: Decimal | None = None            # c5150_KyTagTm
    special_income_tax_payable: Decimal | None = None                    # c5160_TasTmEj
    tonnage_gifts_total: Decimal | None = None                            # c5220_TonnKiKokku


async def generate_tsd_lisa5(
    session: AsyncSession, *, company_id: uuid.UUID, period_start: date, period_end: date,
) -> TsdLisa5Header:
    """**BLOCKED** — Lisa 5 reports gifts/donations/business-entertainment
    expenses. No classified expense/donation source exists — the engine's
    expense records carry no EE gift/donation classification tag today.
    Prerequisite: an EE expense-classification tag distinguishing
    ordinary business expense from a taxable gift/donation/entertainment
    spend (partially derivable from existing expense records with a new
    tag, per build-plan §1.4)."""
    raise NotImplementedError(
        "TSD Lisa 5 generator — blocked on a classified gift/donation/"
        "entertainment expense source (no EE classification tag on "
        "Expense today). See ee-frontier-build-plan.md §1.4."
    )


# ---- Lisa 6 (non-business expenses) ----------------------------------------
# Populated in the official example (header totals + L6_1 tax-base-
# reduction rows + L6_2 related-party rows; L6_3 tonnage-scheme rows are
# NOT populated in the example — modelled anyway since the row shape is
# trivial and the XSD gives an unambiguous 2-field shape).

@dataclass(frozen=True)
class TsdLisa6Row1:
    """Tax base that became no longer applicable (``tsd_L6_1``, XSD
    ``tsdL61``)."""

    month: int          # c6141_Kuu
    year: int            # c6142_Aasta
    amount: Decimal       # c6143_Summa


@dataclass(frozen=True)
class TsdLisa6Row2:
    """Non-business distribution to a related party (``tsd_L6_2``, XSD
    ``tsdL62``)."""

    related_party_code: str                # c6200_Kood
    related_party_name: str                # c6210_Nimi
    country_code: str                      # c6220_RiikKood
    taxable_amount: Decimal                # c6230_MSumma
    payment_type_code: str | None = None   # c6240_ValiKood (minOccurs=0)


@dataclass(frozen=True)
class TsdLisa6Row3:
    """Tonnage-scheme non-business distribution by year (``tsd_L6_3``,
    XSD ``tsdL63``) — NOT populated in the official example; modelled
    from the XSD's own unambiguous 2-field shape only."""

    year: int            # c6300_Aasta
    amount: Decimal       # c6310_Summa


@dataclass(frozen=True)
class TsdLisa6Header:
    """Lisa 6 main-block header totals (``tsdL60``'s ``c60xx``/``c63xx``
    fields) — genuinely "Calculated" by e-MTA (several involve the 22/78
    tax-rate conversion and MIN/MAX clamps the row data alone does not
    determine), so — unlike Lisa 2's totals — these are carried through
    as given, NOT derived here (same posture as ``TsdMainTotals`` for the
    MAIN block: "the roll-up we have")."""

    related_party_value_diff: Decimal | None = None          # c6000_TVahe
    fines_penalties: Decimal | None = None                     # c6010_Trsr
    fines_penalties_to_emta: Decimal | None = None              # c6011_ETrsr
    interest_paid: Decimal | None = None                         # c6020_Intr
    interest_paid_to_emta: Decimal | None = None                  # c6021_EIntr
    seized_assets_value: Decimal | None = None                     # c6030_Kvara
    environmental_charges: Decimal | None = None                    # c6040_Kkt
    environmental_charges_to_emta: Decimal | None = None             # c6041_EKkt
    bribes_kickbacks: Decimal | None = None                           # c6050_Pistis
    non_business_membership_fees: Decimal | None = None                # c6060_Lm
    distributions_missing_source_doc: Decimal | None = None             # c6070_PdokVm
    non_business_expenses_other: Decimal | None = None                   # c6080_KvmMuu
    low_tax_territory_securities_expense: Decimal | None = None           # c6090_Vpk
    low_tax_territory_ownership_expense: Decimal | None = None             # c6100_Osk
    low_tax_territory_penalty_damages: Decimal | None = None                # c6110_Kahj
    low_tax_territory_loan: Decimal | None = None                            # c6120_Laen
    low_tax_territory_credit_loss: Decimal | None = None                     # c6130_KrKah
    tax_base_reduction: Decimal | None = None                                 # c6140_MsVhnd
    total_taxable_amount: Decimal | None = None                                # c6150_SumKokku
    income_tax_payable: Decimal | None = None                                   # c6160_Tasutav
    tonnage_non_business_total: Decimal | None = None                            # c6320_TonnKvmKokku


@dataclass(frozen=True)
class TsdLisa6Listing:
    header: TsdLisa6Header = field(default_factory=TsdLisa6Header)
    rows1: list[TsdLisa6Row1] = field(default_factory=list)
    rows2: list[TsdLisa6Row2] = field(default_factory=list)
    rows3: list[TsdLisa6Row3] = field(default_factory=list)


async def generate_tsd_lisa6(
    session: AsyncSession, *, company_id: uuid.UUID, period_start: date, period_end: date,
) -> TsdLisa6Listing:
    """**BLOCKED** — Lisa 6 reports non-business expenses (fines,
    bribes, seized assets, low-tax-territory dealings, related-party
    distributions). No classification tag exists on the engine's expense
    records distinguishing an ordinary business expense from one of
    these non-business categories. Prerequisite: an EE non-business-
    expense classification tag (same family of gap as Lisa 5's — build-
    plan §1.4 groups L5/L6 together: "partially derivable from expense
    records with an EE classification tag")."""
    raise NotImplementedError(
        "TSD Lisa 6 generator — blocked on a classified non-business-"
        "expense source (no EE classification tag on Expense today). "
        "See ee-frontier-build-plan.md §1.4."
    )


# ---- Lisa 7 (dividends / equity payments) ----------------------------------
# Populated in the official example (header totals + 1b/1C/2/2B/4 row
# lists; 3 is NOT populated — omitted here for the same "no worked
# example to pin against" reason as Lisa 3's unmodelled lists).

@dataclass(frozen=True)
class TsdLisa7Row1b:
    """Foreign income tax withheld/paid, by foreign payer
    (``tsd_L7_1b``, XSD ``tsdL71B``)."""

    payer_regcode: str                # c7101_Regkood
    payer_name: str                   # c7102_Nimi (minOccurs=1)
    payer_country_code: str           # c7103_RiikKood (minOccurs=1)
    income_type_code: str             # c7110_TuliKood
    payment_date: date                # c7120_Kpv
    foreign_income_amount: Decimal    # c7130_VrSumma
    foreign_tax_paid: Decimal         # c7140_VrTasutudTm
    liability_reduction_amount: Decimal | None = None  # c7150_KohustVahendSumma


@dataclass(frozen=True)
class TsdLisa7Row1C:
    """Prior-year dividends taxed at a reduced rate, by year
    (``tsd_L7_1C``, XSD ``tsdL71C``)."""

    year: int          # c7020_Aasta
    amount: Decimal      # c7021_Summa


@dataclass(frozen=True)
class TsdLisa7Row2:
    """Dividends received from a subsidiary, used to fund a tax-exempt
    distribution (``tsd_L7_2``, XSD ``tsdL72``)."""

    payer_regcode: str                    # c7201_Regkood
    payer_name: str                       # c7202_Nimi (minOccurs=1)
    payer_country_code: str               # c7203_RiikKood (minOccurs=1)
    income_type_code: str                 # c7210_TuliKood
    payment_date: date                    # c7220_Kpv
    dividend_participation_percent: Decimal | None = None   # c7230_OsalusDiv
    equity_participation_percent: Decimal | None = None     # c7240_OsalusOmakap
    amount: Decimal | None = None          # c7250_Summa (minOccurs=0)
    foreign_tax_paid: Decimal | None = None       # c7260_VrTasutudTm
    foreign_taxed_profit: Decimal | None = None   # c7270_VrMaksustKasum
    distributed_amount: Decimal | None = None     # c7280_Mvt


@dataclass(frozen=True)
class TsdLisa7Row2B:
    """Disguised profit distribution / CFC / exit-tax funding source
    (``tsd_L7_2B``, XSD ``tsdL72B``)."""

    payer_regcode: str                    # c7201_Regkood
    payer_name: str                       # c7202_Nimi (minOccurs=1)
    payer_country_code: str               # c7203_RiikKood (minOccurs=1)
    income_type_code: str                 # c7210_TuliKood
    payment_date: date                    # c7220_Kpv
    disguised_loan_amount: Decimal | None = None    # c7211_TagLaen
    cfc_funding_amount: Decimal | None = None        # c7215_TagCfc
    exit_tax_funding_amount: Decimal | None = None   # c7216_TagLahkumismaks
    amount: Decimal | None = None          # c7212_Summa (minOccurs=0)
    month: int | None = None               # c7213_Kuu
    year: int | None = None                # c7214_Aasta
    distributed_amount: Decimal | None = None       # c7280_Mvt


@dataclass(frozen=True)
class TsdLisa7Row4:
    """Distributions via a cooperative / association (``tsd_L7_4``, XSD
    ``tsdL74``)."""

    payer_regcode: str                       # c7501_Regkood
    payer_name: str                          # c7502_Nimi (minOccurs=1)
    payer_country_code: str                  # c7503_RiikKood (minOccurs=1)
    cooperative_social_tax: Decimal | None = None    # c7510_AyhSm
    member_social_tax: Decimal | None = None          # c7520_PtkSm
    cooperative_foreign_tax_paid: Decimal | None = None  # c7530_AyhVrTasutudTm
    member_foreign_tax_paid: Decimal | None = None        # c7540_PtkVrTasutudTm
    cooperative_distributed: Decimal | None = None         # c7550_AyhVmt
    member_distributed: Decimal | None = None               # c7560_PtkVmt
    reduced_rate_dividends: Decimal | None = None            # c7580_MmDiv
    credit_institution_prior_advance: Decimal | None = None   # c7590_KredasEelmAastaAvans
    tonnage_dividends: Decimal | None = None                    # c7581_TonnDiv


@dataclass(frozen=True)
class TsdLisa7Header:
    """Lisa 7 main-block header totals (``tsdL70``'s ``c70xx`` fields) —
    same "carried through, not derived" posture as ``TsdLisa6Header``
    (multi-year carry-forward + rate-conversion arithmetic the row data
    alone does not determine). Fields limited to what the official
    example itself populates (build-plan §0.3) — the other ~2
    ``minOccurs=0`` XSD fields (``c7020_OmakapSmEnne2015``,
    ``c7312_MvVmTonnDiv``) are omitted, not guessed."""

    dividends_total: Decimal | None = None                      # c7008_DivKokku
    hidden_distributions: Decimal | None = None                   # c7012_VmKeSum
    assets_taken_out: Decimal | None = None                         # c7014_Lahkumismaks
    cfc_profit: Decimal | None = None                                # c7016_Cfc
    tonnage_dividends_total: Decimal | None = None                    # c7022_TonnDivKokku
    equity_contributions: Decimal | None = None                        # c7030_OmakapSm
    equity_contributions_total: Decimal | None = None                   # c7040_OmakapSmKokku
    equity_contributions_adjusted: Decimal | None = None                 # c7050_OmakapSmKorrig
    equity_distributions_total: Decimal | None = None                     # c7060_OmakapVm
    equity_undistributed_closing: Decimal | None = None                    # c7070_OmakapValjamaksmata
    taxable_excess_over_equity: Decimal | None = None                       # c7080_VmYleSmMaksust
    foreign_tax_withheld_total: Decimal | None = None                        # c7160_VrTasutudTm
    foreign_tax_withheld_adjusted: Decimal | None = None                      # c7170_VrTasutudTmKorrig
    foreign_tax_used: Decimal | None = None                                    # c7180_VrVahendus
    foreign_tax_unused_closing: Decimal | None = None                          # c7190_VrVmTmKasutamata
    income_tax_payable: Decimal | None = None                                   # c7200_TasutavTm
    dividend_equity_income_tax: Decimal | None = None                            # c7217_DivOmakapTm
    income_tax_after_foreign_credit: Decimal | None = None                        # c7218_TmVrVahendus
    income_tax_after_credit_institution: Decimal | None = None                     # c7219_TmKredasVahendus
    exempt_income: Decimal | None = None                                            # c7290_MvVm
    exempt_income_adjusted: Decimal | None = None                                    # c7300_MvVmKorrig
    reduced_rate_dividends_granted_opening: Decimal | None = None                     # c7301_MvMmDivAlgus
    reduced_rate_dividends_received: Decimal | None = None                             # c7302_MvMmDivYa
    tonnage_dividends_received_opening: Decimal | None = None                           # c7303_MvTonnDivAlgus
    tonnage_dividends_received: Decimal | None = None                                    # c7304_MvTonnDivYa
    exempt_dividends_paid_total: Decimal | None = None                                    # c7310_MvVmDiv
    reduced_rate_dividends_paid: Decimal | None = None                                     # c7311_MvVmMmDiv
    exempt_equity_payments: Decimal | None = None                                           # c7320_MvVmOmakap
    exempt_income_unused_closing: Decimal | None = None                                      # c7330_MvVmKasutamata
    reduced_rate_dividends_unused_closing: Decimal | None = None                              # c7331_MvMmDivKasutamata
    tonnage_dividends_unused_closing: Decimal | None = None                                    # c7332_MvTonnDivKasutamata


@dataclass(frozen=True)
class TsdLisa7Listing:
    header: TsdLisa7Header = field(default_factory=TsdLisa7Header)
    rows_1b: list[TsdLisa7Row1b] = field(default_factory=list)
    rows_1c: list[TsdLisa7Row1C] = field(default_factory=list)
    rows_2: list[TsdLisa7Row2] = field(default_factory=list)
    rows_2b: list[TsdLisa7Row2B] = field(default_factory=list)
    rows_4: list[TsdLisa7Row4] = field(default_factory=list)


async def generate_tsd_lisa7(
    session: AsyncSession, *, company_id: uuid.UUID, period_start: date, period_end: date,
) -> TsdLisa7Listing:
    """**BLOCKED** — Lisa 7 reports dividend/equity distributions and
    their withholding treatment. The engine holds no dividend-
    distribution-decision model (no record of a board/shareholder
    decision to distribute, its date, rate treatment, or funding source)
    — build-plan §1.4's own naming: "the engine holds no distribution
    decisions". Prerequisite: a dividend-distribution decision model."""
    raise NotImplementedError(
        "TSD Lisa 7 generator — blocked on a dividend-distribution-"
        "decision source model (no record of board/shareholder "
        "distribution decisions exists in the engine today). See "
        "ee-frontier-build-plan.md §1.4."
    )


__all__ = [
    "PAYMENT_TYPE_WAGES",
    "TsdDataQualityError",
    "TsdLisa1Row",
    "TsdLisa2ARow",
    "TsdLisa2BRow",
    "TsdLisa2InvFondRow",
    "TsdLisa2Listing",
    # Module 1 — Lisa 2-7
    "TsdLisa2MvtRow",
    "TsdLisa2Totals",
    "TsdLisa3Header",
    "TsdLisa4Header",
    "TsdLisa5Header",
    "TsdLisa6Header",
    "TsdLisa6Listing",
    "TsdLisa6Row1",
    "TsdLisa6Row2",
    "TsdLisa6Row3",
    "TsdLisa7Header",
    "TsdLisa7Listing",
    "TsdLisa7Row1C",
    "TsdLisa7Row1b",
    "TsdLisa7Row2",
    "TsdLisa7Row2B",
    "TsdLisa7Row4",
    "TsdListing",
    "TsdMainTotals",
    "compute_lisa2_totals",
    "generate_tsd",
    "generate_tsd_lisa2",
    "generate_tsd_lisa3",
    "generate_tsd_lisa4",
    "generate_tsd_lisa5",
    "generate_tsd_lisa6",
    "generate_tsd_lisa7",
]
