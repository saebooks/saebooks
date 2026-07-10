"""Wave A (2026-07-10) — the ``FLAG_MULTI_CURRENCY`` create-time gate.

The FX engine itself (``rates.py``/``reval.py``/``settle.py``) is fully
built and untouched by this module. What was missing was enforcement:
nothing stopped a below-tier caller from posting a non-base-currency
invoice/bill/expense/payment. This module holds the one shared check
so the four create routes (invoices/bills/expenses/payments) don't each
reimplement "fetch the company's base currency, compare, gate."

A request that stays in the company's own base currency (the common
case — AUD for almost every AU company) is never gated, at any tier:
core single-currency bookkeeping is not a paid feature. Only a request
that *names a different currency* crosses into ``FLAG_MULTI_CURRENCY``
territory (Offline+).
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.services.features import FLAG_MULTI_CURRENCY, require_feature_inline


async def gate_non_base_currency(
    session: AsyncSession,
    request: Request,
    company_id: UUID,
    currency: str,
) -> None:
    """404 (via ``require_feature_inline``) when ``currency`` differs
    from the active company's ``base_currency`` and the caller's
    effective edition doesn't carry ``FLAG_MULTI_CURRENCY``.

    A no-op (no query beyond the base-currency lookup, no exception)
    when ``currency`` matches the company's base currency — the base-
    currency path is never gated, preserving today's AUD-only
    behaviour byte-for-byte at every tier.
    """
    from saebooks.models.company import Company

    base_currency = (
        await session.execute(
            select(Company.base_currency).where(Company.id == company_id)
        )
    ).scalar_one()
    if currency != base_currency:
        require_feature_inline(FLAG_MULTI_CURRENCY, request)
