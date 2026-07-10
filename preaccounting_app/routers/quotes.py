"""Module routes for quotes — thin shell over ``services.quotes``.

Every endpoint runs the SAME engine service function in-process (the module
always has ``PREACCOUNTING_BASE_URL`` unset, so the facade guard in
``services.quotes`` is skipped and the real body executes) against the shared
DB with RLS bound to the ``X-Tenant-Id`` header. Responses are the standard
``QuoteOut`` wire shape so the delegating engine can reconstruct them 1:1.

Error contract (consumed by ``services.quotes`` facade):
* version conflict → 409 ``{"detail","current": QuoteOut}``
* domain error (QuoteError / ValueError) → 422 ``{"code","message"}``
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
from saebooks.api.v1.schemas import QuoteLineCreate, QuoteOut
from saebooks.services import quotes as svc

router = APIRouter(
    prefix="/quotes",
    tags=["preaccounting-quotes"],
    dependencies=[Depends(require_preaccounting_token)],
)


def _dump(quote: Any) -> dict[str, Any]:
    return json.loads(QuoteOut.model_validate(quote).model_dump_json())


def _conflict(exc: svc.VersionConflict) -> JSONResponse:
    return JSONResponse(
        {"detail": "version mismatch", "current": _dump(exc.current)},
        status_code=409,
    )


def _domain_error(exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"code": "quote_error", "message": str(exc)}, status_code=422
    )


# --------------------------------------------------------------------------- #
# Read                                                                         #
# --------------------------------------------------------------------------- #
class GetBody(BaseModel):
    quote_id: uuid.UUID


@router.post("/get")
async def get_quote(
    body: GetBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    q = await svc.api_get(
        session, body.quote_id, tenant_id=ctx.tenant_id, company_id=ctx.company_id
    )
    return JSONResponse(_dump(q) if q is not None else None)


class ListBody(BaseModel):
    customer_id: uuid.UUID | None = None
    status: str | None = None
    since: date | None = None
    expiry_before: date | None = None
    limit: int = 50
    offset: int = 0


@router.post("/list")
async def list_quotes(
    body: ListBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    from saebooks.models.quote import QuoteStatus

    status_enum = QuoteStatus(body.status.upper()) if body.status else None
    assert ctx.company_id is not None, "list requires X-Company-Id"
    rows, total = await svc.list_active(
        session,
        ctx.company_id,
        ctx.tenant_id,
        customer_id=body.customer_id,
        status=status_enum,
        since=body.since,
        expiry_before=body.expiry_before,
        limit=body.limit,
        offset=body.offset,
    )
    return JSONResponse({"items": [_dump(q) for q in rows], "total": total})


# --------------------------------------------------------------------------- #
# Create / update                                                              #
# --------------------------------------------------------------------------- #
class CreateBody(BaseModel):
    actor: str
    customer_id: uuid.UUID
    issue_date: date
    expiry_date: date | None = None
    lines: list[QuoteLineCreate] | None = None
    title: str | None = None
    scope: str | None = None
    notes: str | None = None
    terms: str | None = None
    currency: str = "AUD"
    validity_days: int = 28
    deposit_pct: Decimal | None = None
    late_fee_pct_per_month: Decimal | None = None
    is_supply_only: bool = False


@router.post("/create")
async def create_quote(
    body: CreateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    assert ctx.company_id is not None, "create requires X-Company-Id"
    try:
        q = await svc.api_create(
            session,
            ctx.company_id,
            ctx.tenant_id,
            actor=body.actor,
            customer_id=body.customer_id,
            issue_date=body.issue_date,
            expiry_date=body.expiry_date,
            lines=[ln.model_dump() for ln in body.lines] if body.lines else None,
            title=body.title,
            scope=body.scope,
            notes=body.notes,
            terms=body.terms,
            currency=body.currency,
            validity_days=body.validity_days,
            deposit_pct=(
                body.deposit_pct if body.deposit_pct is not None else Decimal("50")
            ),
            late_fee_pct_per_month=(
                body.late_fee_pct_per_month
                if body.late_fee_pct_per_month is not None
                else Decimal("2.5")
            ),
            is_supply_only=body.is_supply_only,
        )
    except (ValueError, svc.QuoteError) as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(q), status_code=201)


class UpdateBody(BaseModel):
    quote_id: uuid.UUID
    actor: str
    expected_version: int
    force: bool = False
    customer_id: uuid.UUID | None = None
    issue_date: date | None = None
    expiry_date: date | None = None
    title: str | None = None
    scope: str | None = None
    notes: str | None = None
    terms: str | None = None
    currency: str | None = None
    validity_days: int | None = None
    deposit_pct: Decimal | None = None
    late_fee_pct_per_month: Decimal | None = None
    is_supply_only: bool | None = None
    lines: list[QuoteLineCreate] | None = None


@router.post("/update")
async def update_quote(
    body: UpdateBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    try:
        q = await svc.api_update(
            session,
            body.quote_id,
            actor=body.actor,
            expected_version=body.expected_version,
            force=body.force,
            customer_id=body.customer_id,
            issue_date=body.issue_date,
            expiry_date=body.expiry_date,
            title=body.title,
            scope=body.scope,
            notes=body.notes,
            terms=body.terms,
            currency=body.currency,
            validity_days=body.validity_days,
            deposit_pct=body.deposit_pct,
            late_fee_pct_per_month=body.late_fee_pct_per_month,
            is_supply_only=body.is_supply_only,
            lines=[ln.model_dump() for ln in body.lines] if body.lines is not None else None,
            tenant_id=ctx.tenant_id,
        )
    except svc.VersionConflict as exc:
        return _conflict(exc)
    except (ValueError, svc.QuoteError) as exc:
        return _domain_error(exc)
    return JSONResponse(_dump(q))


# --------------------------------------------------------------------------- #
# State transitions (send / accept / decline / archive)                        #
# --------------------------------------------------------------------------- #
class TransitionBody(BaseModel):
    quote_id: uuid.UUID
    actor: str
    expected_version: int


_TRANSITIONS = {
    "send": svc.api_send,
    "accept": svc.api_accept,
    "decline": svc.api_decline,
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
            q = await fn(
                session,
                body.quote_id,
                actor=body.actor,
                expected_version=body.expected_version,
                tenant_id=ctx.tenant_id,
            )
        except svc.VersionConflict as exc:
            return _conflict(exc)
        except (ValueError, svc.QuoteError) as exc:
            return _domain_error(exc)
        return JSONResponse(_dump(q))

    return _handler


for _name in _TRANSITIONS:
    router.add_api_route(f"/{_name}", _make_transition(_name), methods=["POST"])


# --------------------------------------------------------------------------- #
# Conversion                                                                   #
# --------------------------------------------------------------------------- #
class ConvertBody(BaseModel):
    quote_id: uuid.UUID
    actor: str
    expected_version: int


@router.post("/convert-to-invoice")
async def convert_to_invoice(
    body: ConvertBody,
    ctx: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_module_session),
) -> JSONResponse:
    try:
        q, inv = await svc.convert_to_invoice(
            session,
            body.quote_id,
            actor=body.actor,
            expected_version=body.expected_version,
            tenant_id=ctx.tenant_id,
        )
    except svc.VersionConflict as exc:
        return _conflict(exc)
    except (ValueError, svc.QuoteError) as exc:
        return _domain_error(exc)
    return JSONResponse({"quote": _dump(q), "invoice_id": str(inv.id)})
