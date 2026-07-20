"""FX rate cache + translation helpers.

Read-through cache: ``get_rate`` checks ``fx_rate_snapshots`` for the
most recent row at or before ``as_of``; on miss it calls
``fetch_and_cache`` which in turn calls one of the registered fetchers
(RBA for ``source="rba"``). Fetchers are pluggable + mockable — in
tests we register a dict-backed fake and never touch httpx.

Identity short-circuit: ``from_ccy == to_ccy`` always returns
``Decimal("1")`` without touching the cache. The 99% case for AU SMB
(``AUD → AUD``) costs a single dict comparison.

Design: rates stored are the multiplicative factor to convert 1 unit
of ``from_ccy`` into ``to_ccy``. That matches the RBA convention
(1 AUD buys X USD) but we flip it on read if the caller asks for the
reverse pair. The cache column is unambiguous: we always store the
``(from→to)`` rate exactly as fetched — reverse lookups derive from
the inverse at query time rather than double-writing.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.fx_rate_snapshot import FxRateSnapshot
from saebooks.money import round_money

log = logging.getLogger("saebooks.fx")

_ONE = Decimal("1")

RateFetcher = Callable[[str, str, date], Awaitable[Decimal]]

_FETCHERS: dict[str, RateFetcher] = {}


class FxRateError(RuntimeError):
    """Raised when no rate can be found and no fetcher is registered."""


class DocumentBaseTotals(NamedTuple):
    """Result of translating a document's totals to base currency."""

    base_subtotal: Decimal
    base_tax_total: Decimal
    base_total: Decimal


# --------------------------------------------------------------------- #
# Fetcher registry                                                       #
# --------------------------------------------------------------------- #


def register_fetcher(source: str, fetcher: RateFetcher) -> None:
    """Register a fetcher for ``source``. Tests use this to wire a fake."""
    _FETCHERS[source] = fetcher


def clear_fetchers() -> None:
    """Test helper — drop every registered fetcher."""
    _FETCHERS.clear()


async def fetch_and_cache(
    session: AsyncSession,
    *,
    from_ccy: str,
    to_ccy: str,
    as_of: date,
    source: str = "rba",
) -> Decimal:
    """Call the registered fetcher + cache the result."""
    fetcher = _FETCHERS.get(source)
    if fetcher is None:
        raise FxRateError(
            f"No fetcher registered for source={source!r}; either register "
            f"one or seed fx_rate_snapshots manually."
        )
    rate = await fetcher(from_ccy, to_ccy, as_of)
    snap = FxRateSnapshot(
        rate_date=as_of,
        source=source,
        from_ccy=from_ccy,
        to_ccy=to_ccy,
        rate=rate,
    )
    session.add(snap)
    await session.flush()
    return rate


# --------------------------------------------------------------------- #
# Lookup                                                                 #
# --------------------------------------------------------------------- #


async def get_rate(
    session: AsyncSession,
    *,
    from_ccy: str,
    to_ccy: str,
    as_of: date,
    source: str = "rba",
) -> Decimal:
    """Return the rate from ``from_ccy`` → ``to_ccy`` at ``as_of``.

    Read-through cache:
    1. Identity short-circuit returns ``Decimal(1)`` immediately.
    2. Cache hit (direct pair) at ``as_of`` or the most recent earlier
       row returns the cached rate.
    3. Cache hit on the inverse pair returns ``1/rate``.
    4. Miss calls ``fetch_and_cache`` with the registered fetcher.
    """
    from_ccy = from_ccy.upper()
    to_ccy = to_ccy.upper()
    if from_ccy == to_ccy:
        return _ONE

    # Direct pair cache hit (most recent on or before as_of).
    stmt = (
        select(FxRateSnapshot)
        .where(
            FxRateSnapshot.from_ccy == from_ccy,
            FxRateSnapshot.to_ccy == to_ccy,
            FxRateSnapshot.source == source,
            FxRateSnapshot.rate_date <= as_of,
        )
        .order_by(FxRateSnapshot.rate_date.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return Decimal(str(row.rate))

    # Inverse pair cache hit.
    inverse_stmt = (
        select(FxRateSnapshot)
        .where(
            FxRateSnapshot.from_ccy == to_ccy,
            FxRateSnapshot.to_ccy == from_ccy,
            FxRateSnapshot.source == source,
            FxRateSnapshot.rate_date <= as_of,
        )
        .order_by(FxRateSnapshot.rate_date.desc())
        .limit(1)
    )
    inv = (await session.execute(inverse_stmt)).scalar_one_or_none()
    if inv is not None and inv.rate != Decimal("0"):
        return (_ONE / Decimal(str(inv.rate))).quantize(
            Decimal("1.00000000"), rounding=ROUND_HALF_UP
        )

    # Miss — delegate to the registered fetcher.
    return await fetch_and_cache(
        session,
        from_ccy=from_ccy,
        to_ccy=to_ccy,
        as_of=as_of,
        source=source,
    )


# --------------------------------------------------------------------- #
# Translation                                                            #
# --------------------------------------------------------------------- #


def _q2(value: Decimal) -> Decimal:
    return round_money(value)


def apply_document_fx(
    *,
    subtotal: Decimal,
    tax_total: Decimal,
    total: Decimal,
    fx_rate: Decimal,
) -> DocumentBaseTotals:
    """Translate ``subtotal/tax/total`` from document currency to base.

    When ``fx_rate`` is ``Decimal(1)`` the output equals the input
    (AUD→AUD identity). Otherwise each figure is multiplied by the
    rate and quantised to 2dp. We translate each field separately
    instead of deriving ``base_total = base_subtotal + base_tax_total``
    so floor-rounding errors don't drift between the header total and
    the sum of its parts — both are authoritative.
    """
    rate = fx_rate or _ONE
    if rate == _ONE:
        return DocumentBaseTotals(
            base_subtotal=_q2(subtotal),
            base_tax_total=_q2(tax_total),
            base_total=_q2(total),
        )
    return DocumentBaseTotals(
        base_subtotal=_q2(subtotal * rate),
        base_tax_total=_q2(tax_total * rate),
        base_total=_q2(total * rate),
    )
