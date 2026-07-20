"""EE payroll compute — income tax, unemployment insurance, social tax,
pillar II.

kmd-inf-tsd scope Packet 3 (``~/.claude/plans/kmd-inf-tsd-scope.md``
§1.2/§2.2/§7, "the scope's named gate"). Closes scope §0's second
finding: the pay-run engine (``services.pay_runs_v2``) was AU-only —
there was no EE payroll compute anywhere, so a posted EE pay run could
not honestly carry the figures TSD Lisa 1 needs. This module is the
compute; ``services.pay_runs_v2._compute_ee`` is the caller that wires
it into a pay-run line (parallel to the AU path's
``jurisdictions.au.payg``/``jurisdictions.au.super_calc`` split).

Rates come from the reference DB when configured — the SAME seeded rows
KMD/TSD generation will read (``withholding_tables.ee_tsd_income_tax_paye``,
``social_contribution_schemes.ee_social_tax`` / ``ee_unemployment_employee``
/ ``ee_unemployment_employer``, ``mandatory_contribution_rules.ee_pillar_ii_employee_default``)
— falling back to an embedded snapshot when it is not. This mirrors
``tax_return_generator._fetch_box_definitions``'s established convention
exactly, for the same reason: ``REFERENCE_DATABASE_URL`` is unset in the
standard test/CI harness (only ``REFERENCE_MIGRATION_DATABASE_URL``, used
for migrations/seeding — see ``docker-compose.test.yml``), so the
embedded-fallback path is what every test in this repo actually
exercises today. The reference DB is preferred and authoritative
whenever configured+seeded; the fallback is a lock-step snapshot of the
same seed rows, kept in sync by inspection like the box-definition one.

Scope-narrowing, flagged (not silently dropped):

* Both the standard EUR 700/mo and pensionable-age EUR 776/mo basic
  exemption figures (``withholding_tables.yaml``'s codes 610/650) are
  implemented — selected via the explicit
  ``Employee.ee_pensionable_age`` flag (0192_ee_pensionable_age_flag),
  NOT derived from ``Employee.dob``: this module deliberately does not
  invent an old-age-pension-age threshold, which is not sourced
  anywhere in this tree. The flag is caller-supplied ground truth,
  same pattern as ``ee_pillar_ii_rate_percent``.
* Whether the exemption applies AT ALL is a separate, explicit
  per-employee election (``basic_exemption_elected``) — an employee
  files an avaldus electing exactly one employer to apply it; NULL/
  False means NOT applied (the tax-safe default — an unset flag must
  not silently over-claim the exemption and under-withhold). Only
  ``True`` applies it.
* Board-member fees, allowances, and other non-ordinary-wage payment
  classes are NOT modelled here (scope §2.2's own flag) — this module
  computes ordinary gross-wage withholding only.
* **No partial-period proration — flagged, not fixed (critic round 1
  finding).** This module takes no period-length / employment-start /
  employment-end input, so the EUR 700/776 basic exemption and the
  EUR 886/mo social-tax wage-base floor (``social_tax_base = max(gross,
  floor)`` below) are applied IN FULL even for an employee's first or
  last, partial, calendar month (``Employee.start_date``/``end_date``
  are not read anywhere in this compute or in its
  ``pay_runs_v2._compute_ee`` caller). NOT fixed here because the
  correct partial-month treatment (prorate the floor by days? waive it
  entirely? apply in full regardless?) is itself UNVERIFIED against
  Tulumaksuseadus/EMTA — same research-access blocker as the rest of
  this scope (module docstring above) — and guessing a proration rule
  would just trade one unconfirmed behaviour for another with no
  citation. Also pre-existing/systemic: the AU path
  (``jurisdictions.au.payg``/``jurisdictions.au.super_calc``) has the same gap, so this
  is not a regression this module introduced. Deferred to whoever
  confirms the EE ordering; do not treat the current full-floor
  behaviour as correct for a mid-month hire/termination without that
  confirmation.

**UNVERIFIED (flagged per scope §6/"Key UNVERIFIED items", not
silently assumed):** the income-tax withholding ORDERING — this module
applies the basic exemption AND the employee-side deductible
contributions (unemployment 1.6% + pillar II) before the 22% rate,
i.e. ``taxable_base = gross - basic_exemption - unemployment_employee
- pillar_ii``. This ordering was reverse-derived to reproduce the
scope's own golden-month worked figures exactly (§6: E1 gross €500 →
€0 income tax; E2 gross €2,000, pillar-II 6% → pillar-II €120.00,
unemployment €32.00) — it has NOT been independently confirmed against
Tulumaksuseadus / EMTA. Do not treat as authoritative without that
citation.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from saebooks.db import ReferenceSession
from saebooks.models.reference.mandatory_contribution_rule import (
    MandatoryContributionRule,
)
from saebooks.models.reference.social_contribution_scheme import (
    SocialContributionScheme,
)
from saebooks.models.reference.withholding_table import WithholdingTable
from saebooks.money import money_quantum
from saebooks.services.tax_return_generator import _to_reference_jurisdiction

_TWO_PLACES = money_quantum(2)
_HUNDRED = Decimal("100")
_ZERO = Decimal("0")

# Embedded fallback — lock-step snapshot of the EE seed rows (see module
# docstring). Values mirror:
#   seeds/jurisdictions/EE/withholding_tables.yaml (ee_tsd_income_tax_paye)
#   seeds/jurisdictions/EE/social_contribution_schemes.yaml (ee_social_tax /
#     ee_unemployment_employee / ee_unemployment_employer)
#   seeds/jurisdictions/EE/mandatory_contribution_rules.yaml
#     (ee_pillar_ii_employee_default)
_FALLBACK_INCOME_TAX_RATE_PERCENT = Decimal("22.0")
_FALLBACK_BASIC_EXEMPTION_MONTHLY = Decimal("700.00")
_FALLBACK_BASIC_EXEMPTION_MONTHLY_PENSIONABLE = Decimal("776.00")
_FALLBACK_UNEMPLOYMENT_EMPLOYEE_PERCENT = Decimal("1.6")
_FALLBACK_UNEMPLOYMENT_EMPLOYER_PERCENT = Decimal("0.8")
_FALLBACK_SOCIAL_TAX_RATE_PERCENT = Decimal("33.0")
_FALLBACK_SOCIAL_TAX_WAGE_BASE_FLOOR = Decimal("886.00")
_FALLBACK_PILLAR_II_DEFAULT_PERCENT = Decimal("2.0")

# The three elective pillar-II rates the seed defines
# (ee_pillar_ii_employee_default/_4pct/_6pct) — anything else on
# Employee.ee_pillar_ii_rate_percent is a data error, not a silent clamp.
PILLAR_II_ELECTIVE_RATES: tuple[Decimal, ...] = (
    Decimal("2.0"), Decimal("4.0"), Decimal("6.0"),
)


class PayrollEEError(ValueError):
    """Domain-level failure computing an EE payroll line."""


def _q(value: Decimal | int | float | str) -> Decimal:
    """Quantize to 2 dp, half-up — same convention as pay_runs_v2._q /
    the KMD serializer's ``_money`` helper (also UNVERIFIED tie-break,
    see that module)."""
    return Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True)
class EERates:
    """The rate set an EE payroll compute needs for one effective_date.
    ``source`` reports which path served it (``"reference_db"`` |
    ``"embedded_fallback"``) — mirrors
    ``tax_return_generator.TaxReturnResult.source``."""

    income_tax_rate_percent: Decimal
    basic_exemption_monthly: Decimal
    basic_exemption_monthly_pensionable: Decimal
    unemployment_employee_percent: Decimal
    unemployment_employer_percent: Decimal
    social_tax_rate_percent: Decimal
    social_tax_wage_base_floor: Decimal
    pillar_ii_default_percent: Decimal
    source: str


async def _resolve_rates(effective_date: date) -> EERates:
    if ReferenceSession is not None:
        ref_code = _to_reference_jurisdiction("EE")
        async with ReferenceSession() as ref:
            wh_row = (
                await ref.execute(
                    select(WithholdingTable)
                    .where(
                        WithholdingTable.jurisdiction == ref_code,
                        WithholdingTable.code == "ee_tsd_income_tax_paye",
                        WithholdingTable.effective_from <= effective_date,
                    )
                    .order_by(WithholdingTable.effective_from.desc())
                )
            ).scalars().first()

            scheme_result = await ref.execute(
                select(SocialContributionScheme).where(
                    SocialContributionScheme.jurisdiction == ref_code,
                    SocialContributionScheme.code.in_((
                        "ee_social_tax",
                        "ee_unemployment_employee",
                        "ee_unemployment_employer",
                    )),
                    SocialContributionScheme.effective_from <= effective_date,
                )
            )
            by_code: dict[str, SocialContributionScheme] = {}
            for row in scheme_result.scalars().all():
                current = by_code.get(row.code)
                if current is None or row.effective_from > current.effective_from:
                    by_code[row.code] = row

            pillar_row = (
                await ref.execute(
                    select(MandatoryContributionRule)
                    .where(
                        MandatoryContributionRule.jurisdiction == ref_code,
                        MandatoryContributionRule.code == "ee_pillar_ii_employee_default",
                        MandatoryContributionRule.effective_from <= effective_date,
                    )
                    .order_by(MandatoryContributionRule.effective_from.desc())
                )
            ).scalars().first()

            have_all = (
                wh_row is not None
                and pillar_row is not None
                and {"ee_social_tax", "ee_unemployment_employee", "ee_unemployment_employer"}
                <= by_code.keys()
            )
            if have_all:
                params = wh_row.parameters or {}
                social = by_code["ee_social_tax"]
                floor = (
                    social.wage_base_floor
                    if social.wage_base_floor is not None
                    else _FALLBACK_SOCIAL_TAX_WAGE_BASE_FLOOR
                )
                return EERates(
                    income_tax_rate_percent=Decimal(
                        str(params.get("rate_percent", _FALLBACK_INCOME_TAX_RATE_PERCENT))
                    ),
                    basic_exemption_monthly=Decimal(
                        str(params.get(
                            "basic_exemption_monthly_standard",
                            _FALLBACK_BASIC_EXEMPTION_MONTHLY,
                        ))
                    ),
                    basic_exemption_monthly_pensionable=Decimal(
                        str(params.get(
                            "basic_exemption_monthly_pensionable_age",
                            _FALLBACK_BASIC_EXEMPTION_MONTHLY_PENSIONABLE,
                        ))
                    ),
                    unemployment_employee_percent=by_code["ee_unemployment_employee"].rate_percent,
                    unemployment_employer_percent=by_code["ee_unemployment_employer"].rate_percent,
                    social_tax_rate_percent=social.rate_percent,
                    social_tax_wage_base_floor=floor,
                    pillar_ii_default_percent=pillar_row.rate_percent,
                    source="reference_db",
                )
    return EERates(
        income_tax_rate_percent=_FALLBACK_INCOME_TAX_RATE_PERCENT,
        basic_exemption_monthly=_FALLBACK_BASIC_EXEMPTION_MONTHLY,
        basic_exemption_monthly_pensionable=_FALLBACK_BASIC_EXEMPTION_MONTHLY_PENSIONABLE,
        unemployment_employee_percent=_FALLBACK_UNEMPLOYMENT_EMPLOYEE_PERCENT,
        unemployment_employer_percent=_FALLBACK_UNEMPLOYMENT_EMPLOYER_PERCENT,
        social_tax_rate_percent=_FALLBACK_SOCIAL_TAX_RATE_PERCENT,
        social_tax_wage_base_floor=_FALLBACK_SOCIAL_TAX_WAGE_BASE_FLOOR,
        pillar_ii_default_percent=_FALLBACK_PILLAR_II_DEFAULT_PERCENT,
        source="embedded_fallback",
    )


@dataclasses.dataclass(frozen=True)
class EEPayrollResult:
    """Outcome of an EE payroll compute for one employee, one period."""

    gross: Decimal
    income_tax: Decimal
    unemployment_employee: Decimal
    unemployment_employer: Decimal
    social_tax: Decimal
    social_tax_base: Decimal
    """The wage base actually used for social tax — ``max(gross,
    wage_base_floor)``. Kept separate from ``gross`` so a caller/test can
    assert the floor bit explicitly."""
    pillar_ii: Decimal
    pillar_ii_rate_percent: Decimal
    basic_exemption_applied: Decimal
    source: str
    breakdown_note: str


async def compute_ee_payroll(
    *,
    gross: Decimal,
    effective_date: date,
    pillar_ii_rate_percent: Decimal | None = None,
    basic_exemption_elected: bool | None = None,
    pensionable_age: bool | None = None,
    rates: EERates | None = None,
) -> EEPayrollResult:
    """Compute EE income tax + unemployment + social tax + pillar II for
    one employee's gross wage payment.

    Parameters
    ----------
    gross
        Ordinary gross wage for the period (EUR).
    effective_date
        Date used to resolve the in-force rate rows. Ignored if ``rates``
        is supplied.
    rates
        Pre-resolved ``EERates`` (from ``_resolve_rates``). Lets a caller
        that computes many rows for the same ``effective_date`` (e.g.
        ``services.lodgement.tsd.generator``) resolve the reference-DB
        rate rows ONCE and reuse them, instead of opening a fresh
        ``ReferenceSession`` + 3 queries per row. ``None`` (default)
        resolves fresh, as before — fully backward compatible.
    pillar_ii_rate_percent
        The employee's elected pillar-II rate (2 / 4 / 6). ``None`` uses
        the seeded statutory default (2%) — mirrors
        ``Employee.ee_pillar_ii_rate_percent`` NULL semantics.
    basic_exemption_elected
        Whether THIS employer applies the basic exemption (the employee
        files an avaldus electing exactly one employer). ``True``
        applies it; ``None``/``False`` does NOT apply it — mirrors
        ``Employee.ee_basic_exemption_elected`` NULL semantics. This is
        the tax-safe default direction: an unset flag must not silently
        over-claim the exemption and under-withhold.
    pensionable_age
        Selects EUR 776/mo (code 650) instead of EUR 700/mo (code 610)
        when the exemption is applied. ``None``/``False`` = standard
        EUR 700 — mirrors ``Employee.ee_pensionable_age`` NULL
        semantics. Caller-supplied ground truth, not derived from DOB
        (see module docstring).

    Raises
    ------
    PayrollEEError
        Negative gross, or a pillar-II rate outside {2, 4, 6}.
    """
    if gross < 0:
        raise PayrollEEError("gross must be non-negative")
    gross = _q(gross)

    pillar_rate = (
        Decimal(str(pillar_ii_rate_percent))
        if pillar_ii_rate_percent is not None
        else None
    )
    if pillar_rate is not None and pillar_rate not in PILLAR_II_ELECTIVE_RATES:
        raise PayrollEEError(
            f"pillar_ii_rate_percent must be one of "
            f"{[str(r) for r in PILLAR_II_ELECTIVE_RATES]} "
            f"(2% statutory default / 4% / 6% elective) — got {pillar_rate}"
        )

    rates = rates if rates is not None else await _resolve_rates(effective_date)
    effective_pillar_rate = (
        pillar_rate if pillar_rate is not None else rates.pillar_ii_default_percent
    )

    apply_exemption = basic_exemption_elected is True
    if apply_exemption:
        basic_exemption = (
            rates.basic_exemption_monthly_pensionable
            if pensionable_age else rates.basic_exemption_monthly
        )
    else:
        basic_exemption = _ZERO

    unemployment_employee = _q(gross * rates.unemployment_employee_percent / _HUNDRED)
    unemployment_employer = _q(gross * rates.unemployment_employer_percent / _HUNDRED)
    pillar_ii = _q(gross * effective_pillar_rate / _HUNDRED)

    # UNVERIFIED ordering — see module docstring.
    taxable_base = gross - basic_exemption - unemployment_employee - pillar_ii
    if taxable_base < 0:
        taxable_base = _ZERO
    income_tax = _q(taxable_base * rates.income_tax_rate_percent / _HUNDRED)

    # ⚠ Applied in full regardless of a partial employment period (no
    # start_date/end_date input here) — see module docstring's
    # "No partial-period proration" flag, critic round 1 finding.
    social_tax_base = (
        gross if gross > rates.social_tax_wage_base_floor
        else rates.social_tax_wage_base_floor
    )
    social_tax = _q(social_tax_base * rates.social_tax_rate_percent / _HUNDRED)

    breakdown = (
        f"EE payroll ({rates.source}): gross EUR{gross}"
        f" - exemption EUR{basic_exemption}"
        f" - unemployment(employee) EUR{unemployment_employee}"
        f" - pillar-II EUR{pillar_ii}"
        f" = taxable EUR{taxable_base}; income tax "
        f"{rates.income_tax_rate_percent}% = EUR{income_tax}. "
        f"Social tax {rates.social_tax_rate_percent}% x max(EUR{gross}, "
        f"floor EUR{rates.social_tax_wage_base_floor}) = EUR{social_tax}. "
        f"Unemployment(employer) {rates.unemployment_employer_percent}% "
        f"= EUR{unemployment_employer}. Pillar II "
        f"{effective_pillar_rate}% = EUR{pillar_ii}."
    )

    return EEPayrollResult(
        gross=gross,
        income_tax=income_tax,
        unemployment_employee=unemployment_employee,
        unemployment_employer=unemployment_employer,
        social_tax=social_tax,
        social_tax_base=social_tax_base,
        pillar_ii=pillar_ii,
        pillar_ii_rate_percent=effective_pillar_rate,
        basic_exemption_applied=basic_exemption,
        source=rates.source,
        breakdown_note=breakdown,
    )


__all__ = [
    "PILLAR_II_ELECTIVE_RATES",
    "EEPayrollResult",
    "EERates",
    "PayrollEEError",
    "compute_ee_payroll",
    "resolve_ee_rates",
]

# Public alias — internal callers that need to resolve+cache rates ONCE
# across many ``compute_ee_payroll`` calls (e.g.
# ``services.lodgement.tsd.generator``, critic round 1 fix) use this
# instead of the leading-underscore name.
resolve_ee_rates = _resolve_rates
