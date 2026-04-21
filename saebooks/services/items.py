"""Inventory item service — CRUD + WAC receive/issue math.

This module is deliberately small and focused:

* **CRUD** — create / update / get / list / archive, mirroring the
  Project service pattern.
* **Pure math** — :func:`compute_new_wac` is the weighted-average-cost
  blend. Unit-tested with hand-picked fixtures so rounding bugs can't
  sneak in.
* **Movement API** — :func:`receive_stock` (called by
  ``services/bills.py`` after a bill posts) and :func:`issue_stock`
  (called by ``services/invoices.py`` during post) are the only two
  mutating entry points into ``items.on_hand_qty`` + ``items.wac_cost``.

The GL side of a stock movement is NOT posted here — the caller
already has a journal in flight (the bill's or invoice's) and appends
the Dr Inventory / Dr COGS lines directly. This keeps the service
layer free of journaling side-effects and makes the WAC math
verifiable in isolation.

Cost-method policy for v1:

* Only ``CostMethod.WAC`` is supported. ``CostMethod.FIFO`` /
  ``CostMethod.STANDARD`` are reserved for a later batch and the
  Python layer raises on them — the DB CHECK constraint is the
  second line of defence.

Over-issue policy for v1:

* Issuing more units than ``on_hand_qty`` raises
  :class:`ItemError`. Negative stock is disallowed — a real
  accounting package has to decide between "quick sale" ergonomics
  and a clean WAC identity, and v1 picks the clean identity.
  Negative-stock posting is a later-batch feature.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.item import CostMethod, Item
from saebooks.services import audit as audit_svc

_FOURPLACES = Decimal("0.0001")


class ItemError(ValueError):
    """Raised on item validation or stock-movement failure."""


# ---------------------------------------------------------------------- #
# Pure math                                                                #
# ---------------------------------------------------------------------- #


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_FOURPLACES, rounding=ROUND_HALF_UP)


def compute_new_wac(
    *,
    old_on_hand: Decimal,
    old_wac: Decimal,
    received_qty: Decimal,
    received_unit_cost: Decimal,
) -> Decimal:
    """Weighted-average cost after receiving ``received_qty`` at ``received_unit_cost``.

    Formula:

        new_wac = (old_on_hand * old_wac + received_qty * received_unit_cost)
                  / (old_on_hand + received_qty)

    Edge cases:

    * If ``old_on_hand + received_qty`` is zero the new WAC is 0 (no
      stock, no cost — can only happen if the caller receives zero at
      zero stock, which is a no-op anyway).
    * Rounds to 4dp (``NUMERIC(18,4)``) using ROUND_HALF_UP to match
      all other money-adjacent rounding in the codebase.
    """
    if received_qty < Decimal("0"):
        raise ItemError("received_qty must be >= 0")
    if received_unit_cost < Decimal("0"):
        raise ItemError("received_unit_cost must be >= 0")
    if old_on_hand < Decimal("0"):
        raise ItemError("old_on_hand must be >= 0")

    new_on_hand = old_on_hand + received_qty
    if new_on_hand == Decimal("0"):
        return Decimal("0")

    old_value = old_on_hand * old_wac
    received_value = received_qty * received_unit_cost
    return _q4((old_value + received_value) / new_on_hand)


# ---------------------------------------------------------------------- #
# CRUD                                                                     #
# ---------------------------------------------------------------------- #


async def list_items(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Item]:
    stmt = select(Item).where(Item.company_id == company_id)
    if not include_archived:
        stmt = stmt.where(Item.archived_at.is_(None))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(Item.name.ilike(pattern) | Item.sku.ilike(pattern))
    stmt = stmt.order_by(Item.sku).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, item_id: uuid.UUID) -> Item | None:
    return await session.get(Item, item_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    sku: str,
    name: str,
    inventory_account_id: uuid.UUID,
    cogs_account_id: uuid.UUID,
    income_account_id: uuid.UUID,
    description: str | None = None,
    cost_method: CostMethod = CostMethod.WAC,
    on_hand_qty: Decimal = Decimal("0"),
    wac_cost: Decimal = Decimal("0"),
    default_sale_price: Decimal = Decimal("0"),
    extra: dict[str, Any] | None = None,
) -> Item:
    """Create an item. Raises on duplicate ``(company_id, sku)``.

    ``on_hand_qty`` + ``wac_cost`` are exposed so a tiny opening
    balance can be seeded at create time. In practice most users will
    record opening stock via a manual journal or initial bill — which
    is what the posting path enforces consistency for.
    """
    if cost_method != CostMethod.WAC:
        raise ItemError(
            f"Cost method {cost_method} not supported in v1; only WAC."
        )
    if on_hand_qty < Decimal("0"):
        raise ItemError("on_hand_qty must be >= 0")
    if wac_cost < Decimal("0"):
        raise ItemError("wac_cost must be >= 0")

    item = Item(
        company_id=company_id,
        sku=sku.strip(),
        name=name.strip(),
        description=description,
        cost_method=cost_method,
        on_hand_qty=on_hand_qty,
        wac_cost=wac_cost,
        default_sale_price=default_sale_price,
        inventory_account_id=inventory_account_id,
        cogs_account_id=cogs_account_id,
        income_account_id=income_account_id,
        extra=extra,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


_ALLOWED_UPDATE_FIELDS = frozenset({
    "sku",
    "name",
    "description",
    "default_sale_price",
    "inventory_account_id",
    "cogs_account_id",
    "income_account_id",
    "extra",
})


async def update(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    **kwargs: Any,
) -> Item:
    """Update item fields. Only whitelisted fields can be changed.

    ``on_hand_qty`` / ``wac_cost`` / ``cost_method`` are intentionally
    NOT editable through this path — they change only via stock
    movements (:func:`receive_stock` / :func:`issue_stock`) or the
    opening-balance set at create-time. Mutating them directly would
    break the GL↔inventory identity.
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise ItemError(f"Item {item_id} not found")

    if "sku" in kwargs and kwargs["sku"] is not None:
        kwargs["sku"] = kwargs["sku"].strip()
    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()

    before = audit_svc.capture(item)
    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            raise ItemError(f"Cannot update field: {key}")
        setattr(item, key, value)

    await audit_svc.snapshot_row(
        session, item,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    await session.refresh(item)
    return item


async def archive(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> None:
    """Soft-delete. Raises if the item still has on-hand stock — a
    non-zero inventory balance must be written down through a journal
    before the item can be archived, otherwise the GL would be
    orphaned.
    """
    item = await session.get(Item, item_id)
    if item is None:
        return
    if item.on_hand_qty != Decimal("0"):
        raise ItemError(
            f"Cannot archive item {item.sku} while on_hand_qty "
            f"({item.on_hand_qty}) is non-zero; write-off stock first."
        )
    before = audit_svc.capture(item)
    item.archived_at = datetime.now(UTC)
    await audit_svc.snapshot_row(
        session, item,
        action="archive",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()


# ---------------------------------------------------------------------- #
# Stock movements                                                          #
# ---------------------------------------------------------------------- #


async def receive_stock(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    qty: Decimal,
    unit_cost: Decimal,
) -> Item:
    """Receive ``qty`` units at ``unit_cost`` (base currency).

    Updates ``on_hand_qty`` and re-computes ``wac_cost`` via
    :func:`compute_new_wac`. Does NOT commit — callers (``post_bill``)
    are already inside their own transaction and commit once for the
    whole bill.
    """
    if qty <= Decimal("0"):
        raise ItemError(f"receive_stock qty must be > 0, got {qty}")
    item = await session.get(Item, item_id)
    if item is None:
        raise ItemError(f"Item {item_id} not found")
    item.wac_cost = compute_new_wac(
        old_on_hand=item.on_hand_qty,
        old_wac=item.wac_cost,
        received_qty=qty,
        received_unit_cost=unit_cost,
    )
    item.on_hand_qty = _q4(item.on_hand_qty + qty)
    return item


async def issue_stock(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    qty: Decimal,
) -> Decimal:
    """Issue ``qty`` units and return the COGS value ``qty * wac_cost``.

    WAC is unchanged by an issue (only receipts move the average).
    ``on_hand_qty`` decrements by ``qty``. Raises if ``qty`` exceeds
    current ``on_hand_qty`` — negative stock is out of scope for v1.

    Does NOT commit — callers (``post_invoice``) commit once per
    invoice.
    """
    if qty <= Decimal("0"):
        raise ItemError(f"issue_stock qty must be > 0, got {qty}")
    item = await session.get(Item, item_id)
    if item is None:
        raise ItemError(f"Item {item_id} not found")
    if qty > item.on_hand_qty:
        raise ItemError(
            f"Cannot issue {qty} of {item.sku} — only {item.on_hand_qty} "
            "on hand. Receive more stock or reduce the invoice line."
        )
    cogs_value = _q4(qty * item.wac_cost)
    item.on_hand_qty = _q4(item.on_hand_qty - qty)
    return cogs_value
