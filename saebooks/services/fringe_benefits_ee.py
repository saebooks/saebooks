"""EE fringe-benefit (erisoodustus) compute — company car (kW/age basis)
first, generic cash-value benefits second.

kmd-inf-tsd follow-up, Packet 2. ``services.pay_runs_v2._compute_ee``
(EE Packet 3) computes ordinary gross-wage withholding only — fringe
benefits are a SEPARATE EE tax event (Tulumaksuseadus §48): the value of
a non-cash benefit (company car, below-market loan, gift, ...) that the
employer grants an employee attracts income tax AND social tax, both
BORNE BY THE EMPLOYER, with NO effect on the employee's net pay (unlike
ordinary wage withholding, which is deducted FROM the employee). This
module is the compute; ``services.pay_runs_v2._compute_ee`` (Packet 2
wiring) is the caller that attaches it to a pay-run line, parallel to
how that function already calls ``services.payroll_ee.compute_ee_payroll``
for the wage-withholding leg.

Formula — verified directly against EMTA (WebFetch, 2026-07-11):
https://www.emta.ee/en/business-client/taxes-and-payment/income-and-
social-taxes/fringe-benefits::

    income_tax = round(taxable_value * 22 / 78, 2)
    social_tax = round((taxable_value + income_tax) * 33 / 100, 2)

Same 22/78 + 33% figures ``services.payroll_ee`` already resolves for
ordinary wage withholding (``EERates.income_tax_rate_percent`` /
``social_tax_rate_percent``) — this module calls
``payroll_ee.resolve_ee_rates`` rather than re-deriving those two
percentages from a second source (the compound formula is one
engine-wide EE fact, not specific to fringe benefits; reuse, not a
parallel lookup — see ``saebooks/seeds/jurisdictions/EE/
benefit_in_kind_rates.yaml``'s own comment on why ``rate_percent`` there
carries the SAME 22.0000 income-tax numerator). The rounding ORDER
matters and is pinned by EMTA's own worked example (960 taxable value ->
income tax 270.77, social tax 406.15, reproduced exactly in this
module's golden test): income tax is rounded to 2dp FIRST, then the
ROUNDED figure — not the unrounded one — is added into the social-tax
base.

Company-car valuation (EUR-per-kW-per-month) — same EMTA page: "1.96
euros a month per engine power unit (kW) ... In the event of an
automobile older than five years, the price ... is 1.47 euros per
engine power unit (kW)". Rates come from the reference DB
(``benefit_in_kind_rates`` rows ``motor_vehicle`` / ``motor_vehicle_aged``,
added by ``0012_bik_amount_per_unit`` specifically to give this
amount-shaped case a schema home — see that migration + the seed file's
own "GAP CLOSED" comment) when configured, falling back to an embedded
snapshot otherwise — same ``reference_db`` / ``embedded_fallback``
convention as ``services.payroll_ee._resolve_rates``, for the identical
reason (``REFERENCE_DATABASE_URL`` is unset in the standard test/CI
harness).

"Older than five years" is read as STRICTLY greater than 5 (a car
exactly 5 years old still gets the standard 1.96 rate) — EMTA's own
wording ("older than five years"), not "5 years or older".

Scope-narrowing, flagged (not silently dropped):

* No partial-month proration — same posture as ``services.payroll_ee``'s
  own flag for the same class of gap. A benefit granted for only part of
  a calendar month is valued as if for the whole month; caller-level
  proration (if EMTA requires it) is out of scope here.
* ``car_age_years`` is caller-supplied ground truth (not derived from
  any vehicle-registration date on file — no vehicle/asset model exists
  in this tree), same pattern as ``Employee.ee_pillar_ii_rate_percent``.
* Only the company-car (kW/age) and generic cash-value (caller supplies
  the already-determined taxable value) shapes are modelled. Below-
  market loans, waived claims, and the other TSD Lisa 4 categories that
  need their OWN valuation sub-formula (``c4060_SoLaen`` etc.) are not
  computed here — a caller for those must determine the taxable value
  itself and use ``compute_cash_fringe_benefit``.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from saebooks.db import ReferenceSession
from saebooks.models.reference.benefit_in_kind_rate import BenefitInKindRate
from saebooks.money import money_quantum
from saebooks.services.payroll_ee import EERates
from saebooks.services.payroll_ee import resolve_ee_rates as _resolve_wage_rates
from saebooks.services.tax_return_generator import _to_reference_jurisdiction

_TWO_PLACES = money_quantum(2)
_HUNDRED = Decimal("100")

# Embedded fallback — lock-step snapshot of
# saebooks/seeds/jurisdictions/EE/benefit_in_kind_rates.yaml's
# motor_vehicle / motor_vehicle_aged rows.
_FALLBACK_CAR_RATE_STANDARD_EUR_PER_KW = Decimal("1.96")
_FALLBACK_CAR_RATE_AGED_EUR_PER_KW = Decimal("1.47")
_CAR_AGED_THRESHOLD_YEARS = 5


class FringeBenefitEEError(ValueError):
    """Domain-level failure computing an EE fringe-benefit event."""


def _q(value: Decimal | int | float | str) -> Decimal:
    """Quantize to 2dp, half-up — same convention as
    ``pay_runs_v2._q`` / ``payroll_ee._q``."""
    return Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True)
class CarBenefitRates:
    """The EUR/kW/month rate pair for the company-car benefit at one
    ``effective_date``. ``source`` mirrors ``EERates.source``
    (``"reference_db"`` | ``"embedded_fallback"``)."""

    standard_rate_eur_per_kw: Decimal
    aged_rate_eur_per_kw: Decimal
    aged_threshold_years: int
    source: str


async def _resolve_car_rates(effective_date: date) -> CarBenefitRates:
    if ReferenceSession is not None:
        ref_code = _to_reference_jurisdiction("EE")
        async with ReferenceSession() as ref:
            result = await ref.execute(
                select(BenefitInKindRate).where(
                    BenefitInKindRate.jurisdiction == ref_code,
                    BenefitInKindRate.benefit_category.in_(
                        ("motor_vehicle", "motor_vehicle_aged")
                    ),
                    BenefitInKindRate.effective_from <= effective_date,
                )
            )
            by_category: dict[str, BenefitInKindRate] = {}
            for row in result.scalars().all():
                current = by_category.get(row.benefit_category)
                if current is None or row.effective_from > current.effective_from:
                    by_category[row.benefit_category] = row

            standard = by_category.get("motor_vehicle")
            aged = by_category.get("motor_vehicle_aged")
            if (
                standard is not None
                and aged is not None
                and standard.rate_amount_per_unit is not None
                and aged.rate_amount_per_unit is not None
            ):
                return CarBenefitRates(
                    standard_rate_eur_per_kw=standard.rate_amount_per_unit,
                    aged_rate_eur_per_kw=aged.rate_amount_per_unit,
                    aged_threshold_years=_CAR_AGED_THRESHOLD_YEARS,
                    source="reference_db",
                )
    return CarBenefitRates(
        standard_rate_eur_per_kw=_FALLBACK_CAR_RATE_STANDARD_EUR_PER_KW,
        aged_rate_eur_per_kw=_FALLBACK_CAR_RATE_AGED_EUR_PER_KW,
        aged_threshold_years=_CAR_AGED_THRESHOLD_YEARS,
        source="embedded_fallback",
    )


resolve_car_benefit_rates = _resolve_car_rates
resolve_fringe_benefit_tax_rates = _resolve_wage_rates


@dataclasses.dataclass(frozen=True)
class EEFringeBenefitResult:
    """Outcome of one EE fringe-benefit compute — one benefit event,
    one pay period."""

    benefit_category: str
    taxable_value: Decimal
    income_tax: Decimal
    social_tax: Decimal
    total_employer_cost: Decimal
    """income_tax + social_tax — the employer's tax cost of granting
    this benefit (the taxable_value itself is a separate, already-
    incurred cost — e.g. the car's own running costs — not part of
    this total)."""
    engine_power_kw: Decimal | None
    car_age_years: int | None
    rate_used_eur_per_kw: Decimal | None
    valuation_method: str
    source: str
    breakdown_note: str


def _apply_tax_formula(
    taxable_value: Decimal, rates: EERates
) -> tuple[Decimal, Decimal]:
    """income_tax = value * 22/78 (rounded FIRST); social_tax = (value +
    ROUNDED income_tax) * 33% — EMTA's own worked example fixes this
    rounding order (module docstring)."""
    income_tax = _q(
        taxable_value * rates.income_tax_rate_percent / (_HUNDRED - rates.income_tax_rate_percent)
    )
    social_tax = _q(
        (taxable_value + income_tax) * rates.social_tax_rate_percent / _HUNDRED
    )
    return income_tax, social_tax


async def compute_car_fringe_benefit(
    *,
    engine_power_kw: Decimal,
    car_age_years: int,
    effective_date: date,
    tax_rates: EERates | None = None,
    car_rates: CarBenefitRates | None = None,
) -> EEFringeBenefitResult:
    """Company-car (sõiduauto) private-use fringe benefit — EUR/kW/month
    valuation by engine power, reduced past the 5-year boundary (module
    docstring: '>5 years', not '>=5 years').

    Raises
    ------
    FringeBenefitEEError
        Negative ``engine_power_kw`` or negative ``car_age_years``.
    """
    if engine_power_kw < 0:
        raise FringeBenefitEEError("engine_power_kw must be non-negative")
    if car_age_years < 0:
        raise FringeBenefitEEError("car_age_years must be non-negative")

    tax_rates = tax_rates if tax_rates is not None else await _resolve_wage_rates(effective_date)
    car_rates = car_rates if car_rates is not None else await _resolve_car_rates(effective_date)

    aged = car_age_years > car_rates.aged_threshold_years
    rate = car_rates.aged_rate_eur_per_kw if aged else car_rates.standard_rate_eur_per_kw
    taxable_value = _q(Decimal(str(engine_power_kw)) * rate)

    income_tax, social_tax = _apply_tax_formula(taxable_value, tax_rates)

    breakdown = (
        f"EE company-car fringe benefit ({tax_rates.source}/{car_rates.source}): "
        f"{engine_power_kw} kW x EUR{rate}/kW/month "
        f"({'aged >' + str(car_rates.aged_threshold_years) + 'y' if aged else 'standard'}) "
        f"= EUR{taxable_value}. Income tax "
        f"{tax_rates.income_tax_rate_percent}/"
        f"({_HUNDRED - tax_rates.income_tax_rate_percent}) = EUR{income_tax}. "
        f"Social tax ({taxable_value} + {income_tax}) x "
        f"{tax_rates.social_tax_rate_percent}% = EUR{social_tax}."
    )

    return EEFringeBenefitResult(
        benefit_category="motor_vehicle",
        taxable_value=taxable_value,
        income_tax=income_tax,
        social_tax=social_tax,
        total_employer_cost=_q(income_tax + social_tax),
        engine_power_kw=Decimal(str(engine_power_kw)),
        car_age_years=car_age_years,
        rate_used_eur_per_kw=rate,
        valuation_method="amount_per_unit",
        source=f"{tax_rates.source}/{car_rates.source}",
        breakdown_note=breakdown,
    )


async def compute_cash_fringe_benefit(
    *,
    benefit_category: str,
    taxable_value: Decimal,
    effective_date: date,
    tax_rates: EERates | None = None,
) -> EEFringeBenefitResult:
    """Generic cash-value fringe benefit — caller has already determined
    the taxable value (e.g. housing market value, gift cost); this
    applies only the 22/78 + 33% tax formula, no valuation step.

    Raises
    ------
    FringeBenefitEEError
        Negative ``taxable_value`` or an empty ``benefit_category``.
    """
    if taxable_value < 0:
        raise FringeBenefitEEError("taxable_value must be non-negative")
    if not benefit_category:
        raise FringeBenefitEEError("benefit_category is required")

    tax_rates = tax_rates if tax_rates is not None else await _resolve_wage_rates(effective_date)
    value = _q(taxable_value)
    income_tax, social_tax = _apply_tax_formula(value, tax_rates)

    breakdown = (
        f"EE cash-value fringe benefit '{benefit_category}' ({tax_rates.source}): "
        f"value EUR{value}. Income tax "
        f"{tax_rates.income_tax_rate_percent}/"
        f"({_HUNDRED - tax_rates.income_tax_rate_percent}) = EUR{income_tax}. "
        f"Social tax ({value} + {income_tax}) x "
        f"{tax_rates.social_tax_rate_percent}% = EUR{social_tax}."
    )

    return EEFringeBenefitResult(
        benefit_category=benefit_category,
        taxable_value=value,
        income_tax=income_tax,
        social_tax=social_tax,
        total_employer_cost=_q(income_tax + social_tax),
        engine_power_kw=None,
        car_age_years=None,
        rate_used_eur_per_kw=None,
        valuation_method="cost_basis",
        source=tax_rates.source,
        breakdown_note=breakdown,
    )


__all__ = [
    "CarBenefitRates",
    "EEFringeBenefitResult",
    "FringeBenefitEEError",
    "compute_car_fringe_benefit",
    "compute_cash_fringe_benefit",
    "resolve_car_benefit_rates",
    "resolve_fringe_benefit_tax_rates",
]
