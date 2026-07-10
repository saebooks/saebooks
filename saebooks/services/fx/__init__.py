"""FX services — rate lookup + realised gain/loss on settlement.

Public surface:

* ``get_rate(session, from_ccy, to_ccy, as_of, *, source="rba")`` —
  cache-first rate lookup. Returns ``Decimal(1)`` when from_ccy ==
  to_ccy so AUD-only code paths never hit the cache table.
* ``apply_document_fx(session, *, company_id, currency, rate, subtotal,
  tax_total, total)`` — compute the four ``base_*`` totals for a
  document header from its document-currency totals.
* ``realised_gain_lines(inv_rate, pay_rate, alloc_amount, ...)`` —
  compute the Dr/Cr line pair that posts the realised FX difference
  between invoice-issue rate and payment-settle rate.
* ``gate_non_base_currency(session, request, company_id, currency)`` —
  Wave A (2026-07-10) ``FLAG_MULTI_CURRENCY`` enforcement: 404s a
  create request that names a non-base currency below-tier; a no-op
  for base-currency requests at any tier.
"""
from saebooks.services.fx.gate import gate_non_base_currency
from saebooks.services.fx.rates import (
    FxRateError,
    apply_document_fx,
    fetch_and_cache,
    get_rate,
)
from saebooks.services.fx.settle import (
    FxSettleResult,
    compute_realised_fx,
)

__all__ = [
    "FxRateError",
    "FxSettleResult",
    "apply_document_fx",
    "compute_realised_fx",
    "fetch_and_cache",
    "gate_non_base_currency",
    "get_rate",
]
