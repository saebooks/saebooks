"""Realised FX gain/loss on settlement.

When a payment in currency C settles an invoice (or bill) also in
currency C, but the rates at issue-time and payment-time differ, the
base-currency values of the two sides don't match. That gap is a
*realised* FX gain (we got more base) or loss (we got less base).

Math (INCOMING payment against a customer invoice)::

    expected_base_inflow    = alloc_amount * invoice_rate
    actual_base_inflow      = alloc_amount * payment_rate
    gain                    = actual - expected   (positive = gain)

When settlement is in the base currency the rates collapse to 1 and
the gain is 0 — no posting needed.

Sign convention (for an INCOMING receipt, i.e. AR settlement):
* ``gain > 0`` → we received *more* base than the AR said we'd get.
  The AR ledger still holds the old base-value; the extra landed in
  Bank. Post ``Dr Bank (extra)`` + ``Cr Exchange Rate Gain (extra)``.
* ``gain < 0`` → AR cleared for more than we actually received. Post
  ``Dr Exchange Rate Loss (|gain|)`` + ``Cr AR (|gain|)`` so the AR
  balance zeros correctly.

For OUTGOING (AP settlement) the direction flips but the math is
the same with signs reversed; returning just the magnitude + "gain"
flag keeps the service layer simple.

This module is *pure* — it returns the computed amount and direction,
and the caller (``services/payments.py``) decides whether to append
extra Dr/Cr lines to the payment's journal entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from saebooks.money import round_money


def _q2(value: Decimal) -> Decimal:
    return round_money(value)


@dataclass(frozen=True)
class FxSettleResult:
    """Realised FX result for one allocation.

    Attributes
    ----------
    alloc_base_at_document_rate:
        The base-currency value of this allocation if it had been
        translated at the document's (invoice/bill) FX rate.
    alloc_base_at_payment_rate:
        The base-currency value at the payment's FX rate.
    delta:
        ``alloc_base_at_payment_rate - alloc_base_at_document_rate``.
        Positive = we got more base than promised, negative = less.
    is_gain:
        Convenience: ``delta > 0``. When ``delta == 0`` both fields
        read False and ``is_zero`` is True.
    is_zero:
        Convenience: ``delta == 0`` — no FX posting required.
    """

    alloc_base_at_document_rate: Decimal
    alloc_base_at_payment_rate: Decimal
    delta: Decimal
    is_gain: bool
    is_zero: bool


def compute_realised_fx(
    *,
    alloc_amount: Decimal,
    document_rate: Decimal,
    payment_rate: Decimal,
) -> FxSettleResult:
    """Return the realised FX delta for one allocation.

    ``alloc_amount`` is expressed in the *document* currency (which
    equals the payment currency — cross-currency settlement is out of
    scope for v1). Both rates are document-currency → base-currency.
    Identical rates produce ``delta == 0`` and ``is_zero == True``.
    """
    doc_base = _q2(alloc_amount * document_rate)
    pay_base = _q2(alloc_amount * payment_rate)
    delta = pay_base - doc_base
    return FxSettleResult(
        alloc_base_at_document_rate=doc_base,
        alloc_base_at_payment_rate=pay_base,
        delta=delta,
        is_gain=delta > Decimal("0"),
        is_zero=delta == Decimal("0"),
    )
