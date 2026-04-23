"""Pure JSON items router — ``/api/v1/items``.

Phase 1 tier-2 entity. Follows the tax_codes / accounts pattern:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log`` (handled by the service layer).
* Jinja ``/items`` routes remain untouched — same service layer.
* Item is CompanyScoped — uses ``_first_company_id`` helper.
* Extra endpoint: ``GET /api/v1/items/{id}/stock`` — returns stock levels
  for inventory-type items; 404 for service-type items.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    ItemConflictBody,
    ItemCreate,
    ItemListOut,
    ItemOut,
    ItemUpdate,
    StockOut,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.item import Item, ItemType
from saebooks.services import items as svc

router = APIRouter(
    prefix="/items",
    tags=["items"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession) -> UUID:
    """Return the first active company — Phase 1 single-company assumption."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
    return company.id


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _dump(item: Item) -> dict[str, Any]:
    return json.loads(ItemOut.model_validate(item).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ItemListOut)
async def list_items(
    item_type: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> ItemListOut:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        count_stmt = (
            select(func.count())
            .select_from(Item)
            .where(Item.company_id == company_id, Item.archived_at.is_(None))
        )
        if item_type is not None:
            count_stmt = count_stmt.where(Item.item_type == item_type)
        total = (await session.execute(count_stmt)).scalar_one()

        stmt = (
            select(Item)
            .where(Item.company_id == company_id, Item.archived_at.is_(None))
            .order_by(Item.sku)
            .offset(offset)
            .limit(limit)
        )
        if item_type is not None:
            stmt = stmt.where(Item.item_type == item_type)
        items = list((await session.execute(stmt)).scalars().all())
        return ItemListOut(
            items=[ItemOut.model_validate(i) for i in items],
            total=total,
            limit=limit,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{item_id}", response_model=ItemOut)
async def get_item(item_id: UUID) -> ItemOut:
    async with AsyncSessionLocal() as session:
        item = await svc.get(session, item_id)
        if item is None:
            raise HTTPException(404, "Item not found")
        return ItemOut.model_validate(item)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=ItemOut, status_code=201)
async def create_item(
    payload: ItemCreate,
    bearer: str = Depends(require_bearer),
) -> Any:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        try:
            item = await svc.create_for_api(
                session,
                company_id,
                actor=f"api:{bearer[:8]}…",
                tenant_id=tenant_id,
                sku=payload.sku,
                item_type=ItemType(payload.item_type),
                name=payload.name,
                description=payload.description,
                cost_method=payload.cost_method,
                on_hand_qty=payload.on_hand_qty,
                wac_cost=payload.wac_cost,
                default_sale_price=payload.default_sale_price,
                inventory_account_id=payload.inventory_account_id,
                cogs_account_id=payload.cogs_account_id,
                income_account_id=payload.income_account_id,
            )
        except (ValueError, svc.ItemError) as exc:
            raise HTTPException(422, str(exc)) from exc

        await session.refresh(item)
        body = _dump(item)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{item_id}",
    responses={
        200: {"model": ItemOut},
        409: {"model": ItemConflictBody, "description": "Version mismatch"},
    },
)
async def update_item(
    item_id: UUID,
    payload: ItemUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with item version is required")

    async with AsyncSessionLocal() as session:
        try:
            item = await svc.update_with_version(
                session,
                item_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                **payload.model_dump(exclude_unset=True),
            )
        except svc.VersionConflict as exc:
            await session.refresh(exc.current)
            body = ItemConflictBody(
                detail="version mismatch",
                current=ItemOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.ItemError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        await session.refresh(item)
        body = _dump(item)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive via archived_at)
# ---------------------------------------------------------------------------


@router.delete(
    "/{item_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": ItemConflictBody, "description": "Version mismatch"},
    },
)
async def archive_item(
    item_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with item version is required")

    async with AsyncSessionLocal() as session:
        try:
            item = await svc.archive_with_version(
                session,
                item_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        except svc.VersionConflict as exc:
            await session.refresh(exc.current)
            body = ItemConflictBody(
                detail="version mismatch",
                current=ItemOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.ItemError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if item is None:
            raise HTTPException(404, "Item not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Stock endpoint — inventory items only
# ---------------------------------------------------------------------------


@router.get("/{item_id}/stock", response_model=StockOut)
async def get_item_stock(item_id: UUID) -> StockOut:
    """Return current stock levels for an inventory-type item.

    Returns 404 if the item is service-type (no stock tracking).
    Also returns 404 if the item does not exist or is archived.
    """
    async with AsyncSessionLocal() as session:
        item = await svc.get(session, item_id)
        if item is None or item.archived_at is not None:
            raise HTTPException(404, "Item not found")
        # item_type is stored as String — may come back as str or StrEnum
        item_type_str = (
            item.item_type.value
            if hasattr(item.item_type, "value")
            else str(item.item_type)
        )
        if item_type_str != ItemType.INVENTORY.value:
            raise HTTPException(
                404,
                f"Item {item.sku} is a service item — stock endpoint not applicable",
            )
        return StockOut(
            item_id=item.id,
            sku=item.sku,
            item_type=item_type_str,
            on_hand_qty=item.on_hand_qty,
            wac_cost=item.wac_cost,
            inventory_value=(item.on_hand_qty * item.wac_cost).quantize(
                Decimal("0.0001")
            ),
        )
