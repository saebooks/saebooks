"""Module routes for purchase orders — thin shell over ``services.purchase_orders``.

Same contract as the quotes router (see its docstring). Conversion is
PO→bill, the highest-coupling hand-off; it runs the same two-phase
idempotent service code in-process against the shared DB.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from preaccounting_app.deps import (
    TenantContext,
    get_module_session,
    get_tenant_context,
    require_preaccounting_token,
)
from saebooks.api.v1.schemas import PurchaseOrderLineCreate, PurchaseOrderOut
from saebooks.services import purchase_orders as svc

router = APIRouter(
    prefix="/purchase-orders",
    tags=["preaccounting-purchase-orders"],
    dependencies=[Depends(require_preaccounting_token)],
)


def _dump(po: Any) -> dict[str, Any]:
    return json.loads(PurchaseOrderOut.model_validate(po).model_dump_json())


def _conflict(exc: svc.VersionConflict) -> JSONResponse:
    return JSONResponse(
        {"detail": "version mismatch", "current": _dump(exc.current)},
        status_code=409,
    )


def _domain_error(exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"code": "purchase_order_error", "message": str(exc)}, status_code=422
    )


class GetBody(BaseModel):
    po_id: uuid.UUID


@router.post("/get")
async def get_po(
    body: GetBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    po = await svc.api_get(
        session, body.po_id, tenant_id=ctx.tenant_id, company_id=ctx.company_id
    )
    return JSONResponse(_dump(po) if po is not None else None)


class ListBody(BaseModel):
    contact_id: uuid.UUID | None = None
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = 50
    offset: int = 0


@router.post("/list")
async def list_pos(
    body: ListBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    from saebooks.models.purchase_order import PurchaseOrderStatus

    status_enum = PurchaseOrderStatus(body.status.upper()) if body.status else None
    assert ctx.company_id is not None, "list requires X-Company-Id"
    rows, total = await svc.list_active(
        session,
        ctx.company_id,
        ctx.tenant_id,
        contact_id=body.contact_id,
        status=status_enum,
        date_from=body.date_from,
        date_to=body.date_to,
        limit=body.limit,
        offset=body.offset,
    )
    return JSONResponse({"items": [_dump(po) for po in rows], "total": total})


class CreateBody(BaseModel):
    actor: str
    contact_id: uuid.UUID
    issue_date: date
    expected_date: date | None = None
    delivery_address: str | None = None
    lines: list[PurchaseOrderLineCreate] | None = None
    notes: str | None = None
    currency: str = "AUD"
    fx_rate: Decimal | None = None


@router.post("/create")
async def create_po(
    body: CreateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "create requires X-Company-Id"
    try:
        po = await svc.api_create(
            session,
            ctx.company_id,
            ctx.tenant_id,
            actor=body.actor,
            contact_id=body.contact_id,
            issue_date=body.issue_date,
            expected_date=body.expected_date,
            delivery_address=body.delivery_address,
            lines=[ln.model_dump() for ln in body.lines] if body.lines else None,
            notes=body.notes,
            currency=body.currency,
            fx_rate=body.fx_rate,
        )
    except (ValueError, svc.PurchaseOrderError) as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(po), status_code=201)


class UpdateBody(BaseModel):
    po_id: uuid.UUID
    actor: str
    expected_version: int
    force: bool = False
    contact_id: uuid.UUID | None = None
    issue_date: date | None = None
    expected_date: date | None = None
    delivery_address: str | None = None
    notes: str | None = None
    currency: str | None = None
    fx_rate: Decimal | None = None
    lines: list[PurchaseOrderLineCreate] | None = None


@router.post("/update")
async def update_po(
    body: UpdateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    try:
        po = await svc.api_update(
            session,
            body.po_id,
            actor=body.actor,
            expected_version=body.expected_version,
            force=body.force,
            contact_id=body.contact_id,
            issue_date=body.issue_date,
            expected_date=body.expected_date,
            delivery_address=body.delivery_address,
            notes=body.notes,
            currency=body.currency,
            fx_rate=body.fx_rate,
            lines=[ln.model_dump() for ln in body.lines] if body.lines is not None else None,
            tenant_id=ctx.tenant_id,
        )
    except svc.VersionConflict as exc:
        return _conflict(exc)
    except (ValueError, svc.PurchaseOrderError) as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(po))


class TransitionBody(BaseModel):
    po_id: uuid.UUID
    actor: str
    expected_version: int


_TRANSITIONS = {
    "send": svc.api_send,
    "cancel": svc.api_cancel,
    "close": svc.api_close,
    "archive": svc.api_archive,
}


def _make_transition(name: str):
    fn = _TRANSITIONS[name]

    async def _handler(
        body: TransitionBody,
        ctx: TenantContext = Depends(get_tenant_context),
        session: AsyncSession = Depends(get_module_session),
    ) -> JSONResponse:
        try:
            po = await fn(
                session,
                body.po_id,
                actor=body.actor,
                expected_version=body.expected_version,
                tenant_id=ctx.tenant_id,
            )
        except svc.VersionConflict as exc:
            return _conflict(exc)
        except (ValueError, svc.PurchaseOrderError) as exc:
            return _domain_error(exc)
        return JSONResponse(_dump(po))

    return _handler


for _name in _TRANSITIONS:
    router.add_api_route(f"/{_name}", _make_transition(_name), methods=["POST"])


class ConvertBody(BaseModel):
    po_id: uuid.UUID
    actor: str
    expected_version: int
    quantities: dict[int, Decimal] | None = None
    bill_issue_date: date | None = None
    bill_due_date: date | None = None
    supplier_reference: str | None = None


@router.post("/convert-to-bill")
async def convert_to_bill(
    body: ConvertBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    try:
        po, bill = await svc.convert_to_bill(
            session,
            body.po_id,
            actor=body.actor,
            expected_version=body.expected_version,
            tenant_id=ctx.tenant_id,
            quantities=body.quantities,
            bill_issue_date=body.bill_issue_date,
            bill_due_date=body.bill_due_date,
            supplier_reference=body.supplier_reference,
        )
    except svc.VersionConflict as exc:
        return _conflict(exc)
    except (ValueError, svc.PurchaseOrderError) as exc:
        return _domain_error(exc)
    return JSONResponse(
        {
            "purchase_order": _dump(po),
            "bill_id": str(bill.id),
            "bill_number": bill.number,
        }
    )
