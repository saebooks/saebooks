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

Costing-method policy (Wave D, 2026-07-10):

* Costing is a PER-COMPANY setting (``companies.costing_method``,
  ``CostingMethod``), NOT a per-item choice — Richard's decision (2).
  The per-item ``items.cost_method`` column stays ``WAC`` and is
  effectively vestigial; the company setting drives dispatch.
* :func:`receive_stock` / :func:`issue_stock` take a ``method`` kwarg
  (default ``WEIGHTED_AVERAGE`` so every existing direct caller /
  test is unaffected). The bill/invoice posting sites resolve the
  company's method via :func:`get_company_costing_method` and pass it
  in.
* ``WEIGHTED_AVERAGE`` — the pre-Wave-D WAC blend (receipt re-blends
  ``wac_cost``; issue returns COGS at the running average).
* ``FIFO`` — a receipt creates an :class:`InventoryCostLayer`; an
  issue consumes layers oldest-first and returns COGS = sum of the
  consumed layers' cost. ``wac_cost`` is still maintained on receipt
  as a display-only running average (the stock endpoint reads it);
  COGS never uses it under FIFO.
* ``QUANTITY_ONLY`` — receipt/issue adjust ``on_hand_qty`` only;
  issue returns ``0`` so NO COGS/valuation journal is posted (the
  caller guards ``if cogs_value > 0``). ``wac_cost`` is left untouched
  — valuation is whatever the bills recorded.

Over-issue policy for v1:

* Issuing more units than ``on_hand_qty`` raises
  :class:`ItemError`. Negative stock is disallowed — a real
  accounting package has to decide between "quick sale" ergonomics
  and a clean WAC identity, and v1 picks the clean identity.
  Negative-stock posting is a later-batch feature.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.company import Company, CostingMethod
from saebooks.models.inventory_cost_layer import InventoryCostLayer
from saebooks.models.item import CostMethod, Item, ItemType
from saebooks.services import audit as audit_svc
from saebooks.services import change_log as change_log_svc

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
    tenant_id: uuid.UUID | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Item]:
    stmt = select(Item).where(Item.company_id == company_id)
    if tenant_id is not None:
        stmt = stmt.where(Item.tenant_id == tenant_id)
    if not include_archived:
        stmt = stmt.where(Item.archived_at.is_(None))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(Item.name.ilike(pattern) | Item.sku.ilike(pattern))
    stmt = stmt.order_by(Item.sku).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Item | None:
    """Fetch an item by id.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    Keyword-only + optional so existing callers keep working unchanged.
    """
    if tenant_id is None and company_id is None:
        return await session.get(Item, item_id)
    clauses = [Item.id == item_id]
    if tenant_id is not None:
        clauses.append(Item.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Item.company_id == company_id)
    result = await session.execute(
        select(Item).where(*clauses)
    )
    return result.scalars().first()


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


async def get_company_costing_method(
    session: AsyncSession, company_id: uuid.UUID
) -> CostingMethod:
    """Resolve the company's inventory costing policy.

    Falls back to ``WEIGHTED_AVERAGE`` when the company is missing
    (defensive — the FK guarantees it exists in practice), preserving
    the pre-Wave-D default.
    """
    value = await session.scalar(
        select(Company.costing_method).where(Company.id == company_id)
    )
    if value is None:
        return CostingMethod.WEIGHTED_AVERAGE
    return CostingMethod(value)


async def _consume_fifo_layers(
    session: AsyncSession, item: Item, qty: Decimal
) -> Decimal:
    """Consume ``qty`` units from ``item``'s cost layers oldest-first.

    Returns the COGS = sum over consumed layers of
    ``consumed_units * layer.unit_cost``. Fully-consumed layers are
    left as ``remaining_qty = 0`` rows (audit history), not deleted.

    Coverage gap: if the open layers don't cover ``qty`` (e.g. opening
    stock seeded at create-time carries no layer, or a company switched
    to FIFO mid-life), the shortfall is valued at ``item.wac_cost`` (the
    running-average display cost) so the ledger stays balanced and the
    issue never spuriously fails — the real over-issue guard is the
    ``on_hand_qty`` check in :func:`issue_stock`.
    """
    remaining = qty
    cogs = Decimal("0")
    layers = (
        await session.execute(
            select(InventoryCostLayer)
            .where(
                InventoryCostLayer.item_id == item.id,
                InventoryCostLayer.remaining_qty > Decimal("0"),
            )
            # Oldest-first: received_date is the primary FIFO key;
            # created_at breaks ties for same-date receipts so they consume
            # in receipt order; id is the final deterministic tiebreak.
            .order_by(
                InventoryCostLayer.received_date,
                InventoryCostLayer.created_at,
                InventoryCostLayer.id,
            )
        )
    ).scalars().all()
    for layer in layers:
        if remaining <= Decimal("0"):
            break
        take = min(layer.remaining_qty, remaining)
        cogs += take * layer.unit_cost
        layer.remaining_qty = _q4(layer.remaining_qty - take)
        remaining = _q4(remaining - take)
    if remaining > Decimal("0"):
        # Un-layered opening stock — value the remainder at the running
        # average (0 if never set) so double-entry stays balanced.
        cogs += remaining * item.wac_cost
    return _q4(cogs)


async def receive_stock(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    qty: Decimal,
    unit_cost: Decimal,
    method: CostingMethod = CostingMethod.WEIGHTED_AVERAGE,
    received_date: date | None = None,
) -> Item:
    """Receive ``qty`` units at ``unit_cost`` (base currency).

    Dispatches on the company's ``method``:

    * ``WEIGHTED_AVERAGE`` / ``FIFO`` — re-computes ``wac_cost`` via
      :func:`compute_new_wac` (a running average; for FIFO it is a
      display-only figure, COGS comes from layers).
    * ``FIFO`` additionally creates an :class:`InventoryCostLayer`.
    * ``QUANTITY_ONLY`` — leaves ``wac_cost`` untouched.

    Always increments ``on_hand_qty``. Does NOT commit — callers
    (``post_bill``) are already inside their own transaction and commit
    once for the whole bill.
    """
    if qty <= Decimal("0"):
        raise ItemError(f"receive_stock qty must be > 0, got {qty}")
    item = await session.get(Item, item_id)
    if item is None:
        raise ItemError(f"Item {item_id} not found")

    if method != CostingMethod.QUANTITY_ONLY:
        item.wac_cost = compute_new_wac(
            old_on_hand=item.on_hand_qty,
            old_wac=item.wac_cost,
            received_qty=qty,
            received_unit_cost=unit_cost,
        )
    item.on_hand_qty = _q4(item.on_hand_qty + qty)

    if method == CostingMethod.FIFO:
        session.add(
            InventoryCostLayer(
                company_id=item.company_id,
                tenant_id=item.tenant_id,
                item_id=item.id,
                received_date=received_date or datetime.now(UTC).date(),
                original_qty=_q4(qty),
                remaining_qty=_q4(qty),
                unit_cost=_q4(unit_cost),
            )
        )
    return item


async def issue_stock(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    qty: Decimal,
    method: CostingMethod = CostingMethod.WEIGHTED_AVERAGE,
) -> Decimal:
    """Issue ``qty`` units and return the COGS value for the company's method.

    * ``WEIGHTED_AVERAGE`` — COGS = ``qty * wac_cost`` (WAC unchanged by
      an issue; only receipts move the average).
    * ``FIFO`` — COGS = sum of consumed layers, oldest-first (see
      :func:`_consume_fifo_layers`).
    * ``QUANTITY_ONLY`` — COGS = ``0`` so the caller posts NO
      COGS/valuation journal (it guards ``if cogs_value > 0``).

    ``on_hand_qty`` decrements by ``qty`` in every case. Raises if
    ``qty`` exceeds current ``on_hand_qty`` — negative stock is out of
    scope for v1. Does NOT commit — callers (``post_invoice``) commit
    once per invoice.
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

    if method == CostingMethod.FIFO:
        cogs_value = await _consume_fifo_layers(session, item, qty)
    elif method == CostingMethod.QUANTITY_ONLY:
        cogs_value = Decimal("0")
    else:  # WEIGHTED_AVERAGE
        cogs_value = _q4(qty * item.wac_cost)

    item.on_hand_qty = _q4(item.on_hand_qty - qty)
    return cogs_value


# ---------------------------------------------------------------------------
# API-oriented helpers (version-aware, change_log wiring)
# The Jinja-facing functions above remain untouched.
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value.

    The API layer catches this and returns 409 with current server state.
    """

    def __init__(self, current: Item) -> None:
        super().__init__(
            f"Item {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# Columns serialised into change_log.payload
_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "sku",
    "item_type",
    "name",
    "description",
    "cost_method",
    "on_hand_qty",
    "wac_cost",
    "default_sale_price",
    "inventory_account_id",
    "cogs_account_id",
    "income_account_id",
    "version",
    "created_at",
    "archived_at",
)


def _serialise(item: Item) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _ITEM_COLUMNS:
        val = getattr(item, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create_for_api(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    sku: str,
    name: str,
    inventory_account_id: uuid.UUID,
    cogs_account_id: uuid.UUID,
    income_account_id: uuid.UUID,
    item_type: ItemType = ItemType.INVENTORY,
    description: str | None = None,
    cost_method: CostMethod = CostMethod.WAC,
    on_hand_qty: Decimal = Decimal("0"),
    wac_cost: Decimal = Decimal("0"),
    default_sale_price: Decimal = Decimal("0"),
    extra: dict[str, Any] | None = None,
    actor: str = "api",
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> Item:
    """Create an item and append a change_log row."""
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
        tenant_id=tenant_id,
        sku=sku.strip(),
        item_type=item_type,
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
        version=1,
    )
    session.add(item)
    await session.flush()
    await session.refresh(item)
    await change_log_svc.append(
        session,
        entity="item",
        entity_id=item.id,
        op="create",
        actor=actor,
        payload=_serialise(item),
        version=item.version,
    )
    await session.commit()
    return item


async def update_with_version(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    expected_version: int | None = None,
    actor: str | None = None,
    **kwargs: Any,
) -> Item:
    """Update item fields with optimistic locking + change_log.

    Only whitelisted fields can be changed (same list as the Jinja ``update``
    function). ``on_hand_qty`` / ``wac_cost`` / ``cost_method`` / ``item_type``
    are intentionally not editable through this path.
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise ItemError(f"Item {item_id} not found")

    if expected_version is not None and item.version != expected_version:
        raise VersionConflict(item)

    if "sku" in kwargs and kwargs["sku"] is not None:
        kwargs["sku"] = kwargs["sku"].strip()
    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()

    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            raise ItemError(f"Cannot update field: {key}")
        setattr(item, key, value)

    item.version = item.version + 1
    await session.flush()
    await session.refresh(item)
    await change_log_svc.append(
        session,
        entity="item",
        entity_id=item.id,
        op="update",
        actor=actor or "api",
        payload=_serialise(item),
        version=item.version,
    )
    await session.commit()
    return item


async def archive_with_version(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    expected_version: int | None = None,
    actor: str | None = None,
) -> Item | None:
    """Soft-archive an item with optimistic locking + change_log.

    Raises ``ItemError`` if the item still has on-hand stock, consistent
    with the Jinja-facing ``archive`` function.
    """
    item = await session.get(Item, item_id)
    if item is None:
        return None
    if expected_version is not None and item.version != expected_version:
        raise VersionConflict(item)
    if item.on_hand_qty != Decimal("0"):
        raise ItemError(
            f"Cannot archive item {item.sku} while on_hand_qty "
            f"({item.on_hand_qty}) is non-zero; write-off stock first."
        )
    item.archived_at = datetime.now(UTC)
    item.version = item.version + 1
    await session.flush()
    await session.refresh(item)
    await change_log_svc.append(
        session,
        entity="item",
        entity_id=item.id,
        op="archive",
        actor=actor or "api",
        payload=_serialise(item),
        version=item.version,
    )
    await session.commit()
    return item
