"""Currency minor-unit precision primitives (M1.5 slice 5-PRIMITIVES).

Money is stored at ``Numeric(18, 4)`` (see ``saebooks.db_types.Money``)
so sub-cent currencies fit, but every value is *rounded* to its
currency's ISO-4217 minor unit — 2 places for AUD, 0 for JPY, 3 for
BHD. The helpers here are the single source of that rounding:

* ``money_quantum(places)`` — the ``Decimal`` quantum for a minor-unit
  count. ``money_quantum(2) == Decimal("0.01")`` byte-identically, so
  swapping a hardcoded ``Decimal("0.01")`` for it never changes AU
  output, whatever rounding mode the call site uses.
* ``round_money(value, currency_or_places)`` — ROUND_HALF_UP to the
  currency's minor unit. With the default (2 places / AUD) it is
  byte-identical to the historical ``quantize(Decimal("0.01"),
  ROUND_HALF_UP)`` idiom.
* ``decimal_places_for(code)`` — minor units for an ISO-4217 code,
  defaulting to 2 when the code is unknown or absent (every AU/base
  path therefore behaves exactly as before).

The static minor-unit map mirrors the reference-DB seed at
``saebooks/seeds/jurisdictions/_global/currencies.yaml`` — keep the
two in sync. Callers that already hold a ``Currency`` row can pass its
``decimal_places`` int directly instead of the code.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

DEFAULT_DECIMAL_PLACES = 2

# ISO-4217 currencies whose minor unit is NOT the 2-place default.
# Zero-decimal and three-decimal currencies per the ISO-4217 registry.
_NON_DEFAULT_MINOR_UNITS: dict[str, int] = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0,
    "KMF": 0, "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0,
    "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3,
    "TND": 3,
}

_QUANTA: dict[int, Decimal] = {}


def decimal_places_for(currency_code: str | None) -> int:
    """Minor units for an ISO-4217 code; 2 when unknown/absent (AU default)."""
    if not currency_code:
        return DEFAULT_DECIMAL_PLACES
    return _NON_DEFAULT_MINOR_UNITS.get(
        currency_code.upper(), DEFAULT_DECIMAL_PLACES
    )


def money_quantum(decimal_places: int = DEFAULT_DECIMAL_PLACES) -> Decimal:
    """Quantize target for a minor-unit count: 2 → 0.01, 0 → 1, 3 → 0.001."""
    quantum = _QUANTA.get(decimal_places)
    if quantum is None:
        quantum = _QUANTA[decimal_places] = Decimal(1).scaleb(-decimal_places)
    return quantum


def round_money(
    value: Decimal,
    currency_or_places: str | int | None = DEFAULT_DECIMAL_PLACES,
) -> Decimal:
    """ROUND_HALF_UP ``value`` to a currency's minor unit.

    ``currency_or_places`` is an ISO-4217 code, an explicit minor-unit
    count (e.g. a ``Currency.decimal_places`` value), or None (→ 2).
    """
    if isinstance(currency_or_places, int):
        places = currency_or_places
    else:
        places = decimal_places_for(currency_or_places)
    return value.quantize(money_quantum(places), rounding=ROUND_HALF_UP)
