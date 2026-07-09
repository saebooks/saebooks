"""Proration math — the shared primitive behind invoice/recurring/billing prorate.

There are four user-facing prorate flows in saebooks:

1. **First-period recurring** — a customer signs up mid-cycle on a
   monthly/annual subscription. The first invoice covers the partial
   period only. (See ``preview_first_period_invoice`` and the
   ``/proration/recurring/{id}/first-period`` API route.)

2. **Mid-period plan-change** — a customer on a $99/mo plan upgrades to
   $149/mo on day 12 of a 30-day period. The change-day adjustment is
   a credit for the unused portion of the old plan plus a charge for
   the unused portion of the new plan.

3. **Per-line date-range** — an ad-hoc invoice line covers a partial
   period (e.g. "13 days of January rent"). The full-period amount and
   the date range determine the prorated subtotal.

4. **Deferred revenue recognition** — for already-issued invoices whose
   service span >1 calendar month, the income side is recognised
   monthly. (Lives in ``services/deferred_revenue.py``.)

This module owns the pure-math primitives that all four flows share:
``prorate_amount``, ``days_in_basis_period``, ``ProrateBasis``. Each
flow's higher-level orchestration lives next to its data model
(invoices, recurring_invoices, deferred_revenue).

Rounding: every public function returns ``Decimal`` quantised to 2 dp
ROUND_HALF_UP. Callers that want full precision (e.g. for a downstream
multiplication) should use the ``factor`` returned by
``prorate_factor`` and round once at the end.
"""
from __future__ import annotations

import enum
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_TWOPLACES = Decimal("0.01")
_FOURPLACES = Decimal("0.0001")


class ProrateBasis(enum.StrEnum):
    """The "denominator" period the full amount represents."""

    DAILY = "DAILY"          # full_amount is 1 day; rare, but present for completeness
    WEEKLY = "WEEKLY"        # 7 days
    MONTHLY = "MONTHLY"      # actual days in the calendar month containing service_start
    QUARTERLY = "QUARTERLY"  # 3 calendar months from service_start
    ANNUAL = "ANNUAL"        # 365 / 366 — actual days from service_start to one year forward


class ProrationError(ValueError):
    """Raised when proration inputs are inconsistent (e.g. end < start)."""


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_FOURPLACES, rounding=ROUND_HALF_UP)


def days_inclusive(start: date, end: date) -> int:
    """Number of days the service period covers (start and end both billable)."""
    if end < start:
        raise ProrationError(
            f"service_end_date {end} is before service_start_date {start}"
        )
    return (end - start).days + 1


def days_in_basis_period(
    basis: ProrateBasis, anchor: date
) -> int:
    """Return the day-count of the *full* period the prorate is sliced from.

    For MONTHLY: the calendar-month length containing ``anchor``
    (28/29/30/31). For QUARTERLY: 3 calendar months from ``anchor``.
    For ANNUAL: 365 or 366 depending on whether the next anchor-day-1-year
    forward is a leap day.
    """
    if basis is ProrateBasis.DAILY:
        return 1
    if basis is ProrateBasis.WEEKLY:
        return 7
    if basis is ProrateBasis.MONTHLY:
        return monthrange(anchor.year, anchor.month)[1]
    if basis is ProrateBasis.QUARTERLY:
        # 3 calendar months from anchor; total days = sum of those 3 months
        total = 0
        y, m = anchor.year, anchor.month
        for _ in range(3):
            total += monthrange(y, m)[1]
            m += 1
            if m > 12:
                m = 1
                y += 1
        return total
    if basis is ProrateBasis.ANNUAL:
        try:
            next_anchor = anchor.replace(year=anchor.year + 1)
        except ValueError:
            # 29 Feb on a non-leap-year following year — fall to 28 Feb
            next_anchor = anchor.replace(year=anchor.year + 1, day=28)
        return (next_anchor - anchor).days
    raise ProrationError(f"Unknown ProrateBasis {basis!r}")


def prorate_factor(
    basis: ProrateBasis,
    service_start: date,
    service_end: date,
) -> Decimal:
    """Return the fraction (0..1) of a full ``basis`` period that
    ``service_start..service_end`` covers.

    Quantised to 4 dp so a downstream multiplication doesn't compound
    rounding error before the final 2-dp quantise.
    """
    days = Decimal(days_inclusive(service_start, service_end))
    full = Decimal(days_in_basis_period(basis, service_start))
    if full == 0:
        raise ProrationError("days_in_basis_period returned 0")
    return _q4(days / full)


def prorate_amount(
    full_period_amount: Decimal,
    basis: ProrateBasis,
    service_start: date,
    service_end: date,
) -> Decimal:
    """Return ``full_period_amount`` scaled by the
    ``service_start..service_end`` prorate factor, quantised to 2 dp.

    The internal multiplication keeps full precision (days × amount /
    full_days) and quantises once. Going through a rounded
    ``prorate_factor`` would leak ~1c per line on awkward fractions.
    """
    if not isinstance(full_period_amount, Decimal):
        full_period_amount = Decimal(str(full_period_amount))
    days = Decimal(days_inclusive(service_start, service_end))
    full = Decimal(days_in_basis_period(basis, service_start))
    if full == 0:
        raise ProrationError("days_in_basis_period returned 0")
    return _q2(full_period_amount * days / full)


# --------------------------------------------------------------------------- #
# Mid-period plan-change
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanChangeAdjustment:
    """Result of computing a mid-period plan-change.

    ``credit`` and ``charge`` are both non-negative. The caller
    (a recurring-invoice or invoice service) translates these into either
    one invoice with two lines, or one credit-note plus one invoice,
    depending on net direction and policy.
    """

    period_start: date
    period_end: date
    change_date: date
    days_total: int
    days_used: int          # change_date − period_start (inclusive of period_start, exclusive of change_date)
    days_remaining: int     # period_end − change_date + 1 (inclusive of change_date, inclusive of period_end)
    credit: Decimal         # unused portion of the OLD plan to refund
    charge: Decimal         # unused portion of the NEW plan to bill
    net: Decimal            # charge − credit; positive = customer owes more


def plan_change_adjustment(
    old_period_amount: Decimal,
    new_period_amount: Decimal,
    period_start: date,
    period_end: date,
    change_date: date,
) -> PlanChangeAdjustment:
    """Compute the credit (old plan, unused) and charge (new plan, unused)
    when a customer switches plans on ``change_date`` inside a billing
    period that runs ``period_start..period_end`` (both inclusive).

    Day-count is by inclusive days, so a 30-day month with change_date
    halfway has days_used=15, days_remaining=15.
    """
    if period_end < period_start:
        raise ProrationError(
            f"period_end {period_end} is before period_start {period_start}"
        )
    if change_date < period_start or change_date > period_end:
        raise ProrationError(
            f"change_date {change_date} must lie within "
            f"{period_start}..{period_end}"
        )

    if not isinstance(old_period_amount, Decimal):
        old_period_amount = Decimal(str(old_period_amount))
    if not isinstance(new_period_amount, Decimal):
        new_period_amount = Decimal(str(new_period_amount))

    days_total = (period_end - period_start).days + 1
    # days_used = days the customer used the OLD plan: from period_start
    #             through (change_date - 1 day) inclusive, i.e. (change_date - period_start) days.
    days_used = (change_date - period_start).days
    days_remaining = days_total - days_used

    if days_total == 0:
        raise ProrationError("days_total is 0 — period_start == period_end + 1?")

    factor_remaining = Decimal(days_remaining) / Decimal(days_total)
    credit = _q2(old_period_amount * factor_remaining)
    charge = _q2(new_period_amount * factor_remaining)
    net = _q2(charge - credit)

    return PlanChangeAdjustment(
        period_start=period_start,
        period_end=period_end,
        change_date=change_date,
        days_total=days_total,
        days_used=days_used,
        days_remaining=days_remaining,
        credit=credit,
        charge=charge,
        net=net,
    )


# --------------------------------------------------------------------------- #
# First-period recurring prorate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FirstPeriodProrate:
    """Result of pricing the first (partial) invoice in a recurring template.

    ``prorated_amount`` is the unit_price the caller should use on the
    *first* invoice line. Subsequent invoices use the un-prorated
    ``full_period_amount`` straight from the recurring template.
    """

    full_period_amount: Decimal
    basis: ProrateBasis
    service_start: date
    service_end: date
    days_used: int
    days_in_full: int
    factor: Decimal
    prorated_amount: Decimal


def first_period_prorate(
    full_period_amount: Decimal,
    basis: ProrateBasis,
    service_start: date,
    service_end: date,
) -> FirstPeriodProrate:
    """Convenience wrapper around ``prorate_factor``/``prorate_amount``
    that returns the full breakdown the recurring-invoice generator
    needs to build a transparent first invoice (line description
    "Pro-rata 14 of 30 days" etc.)."""
    if not isinstance(full_period_amount, Decimal):
        full_period_amount = Decimal(str(full_period_amount))

    days_used = days_inclusive(service_start, service_end)
    days_in_full = days_in_basis_period(basis, service_start)
    factor = prorate_factor(basis, service_start, service_end)
    # Compute prorated amount at full precision (not via rounded factor)
    # to avoid ~1c rounding leak — see ``prorate_amount``.
    prorated = _q2(
        full_period_amount * Decimal(days_used) / Decimal(days_in_full)
    )

    return FirstPeriodProrate(
        full_period_amount=full_period_amount,
        basis=basis,
        service_start=service_start,
        service_end=service_end,
        days_used=days_used,
        days_in_full=days_in_full,
        factor=factor,
        prorated_amount=prorated,
    )


# --------------------------------------------------------------------------- #
# Per-line date-range — the simplest case: just ``prorate_amount``
# --------------------------------------------------------------------------- #
# (The ``per-line date-range`` flow is just a direct call to
# ``prorate_amount`` from the caller — no extra orchestration needed
# beyond the API/UI shim that turns a request body into the right call.)


def basis_from_string(value: str) -> ProrateBasis:
    """Tolerant parser used by API request schemas."""
    if value is None:
        raise ProrationError("basis is required")
    cleaned = value.strip().upper()
    try:
        return ProrateBasis(cleaned)
    except ValueError as exc:
        raise ProrationError(
            f"Unknown prorate basis {value!r}; "
            f"valid: {[b.value for b in ProrateBasis]}"
        ) from exc


__all__ = [
    "FirstPeriodProrate",
    "PlanChangeAdjustment",
    "ProrateBasis",
    "ProrationError",
    "basis_from_string",
    "days_in_basis_period",
    "days_inclusive",
    "first_period_prorate",
    "plan_change_adjustment",
    "prorate_amount",
    "prorate_factor",
]
