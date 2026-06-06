"""JSON router — ``/api/v1/receipts``.

Generic money-in record (refunds / cashbacks / rebates / ATO GST refund /
insurance recovery not tied to a bill). Dr bank/asset, Cr income|expense,
Cr GST. See ``services/receipts.py``.

Endpoints (mirror credit_notes / supplier_credit_notes):

* ``POST   /receipts``               — create a draft.
* ``GET    /receipts``               — list (filter by status).
* ``GET    /receipts/{id}``          — single.
* ``PATCH  /receipts/{id}``          — edit a draft (If-Match).
* ``POST   /receipts/{id}/post``     — DRAFT -> POSTED (If-Match).
* ``POST   /receipts/{id}/void``     — POSTED -> VOIDED (If-Match).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import (
    get_active_company_id,
    get_active_user_id,
    get_session,
)
from saebooks.models.receipt import Receipt, ReceiptStatus
from saebooks.services import receipts as svc

router = APIRouter(
    prefix="/receipts",
    tags=["receipts"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ReceiptLineIn(BaseModel):
    description: str = Field(min_length=1)
    account_id: UUID
    tax_code_id: UUID | None = None
    amount: Decimal = Field(default=Decimal("0"))


class ReceiptCreate(BaseModel):
    bank_account_id: UUID
    receipt_date: date
    lines: list[ReceiptLineIn] = Field(default_factory=list)
    contact_id: UUID | None = None
    reference: str | None = Field(default=None, max_length=255)
    reason: str | None = None
    notes: str | None = None


class ReceiptUpdate(BaseModel):
    bank_account_id: UUID | None = None
    receipt_date: date | None = None
    lines: list[ReceiptLineIn] | None = None
    contact_id: UUID | None = None
    reference: str | None = Field(default=None, max_length=255)
    reason: str | None = None
    notes: str | None = None


class ReceiptLineOut(BaseModel):
    id: UUID
    line_no: int
    description: str
    account_id: UUID
    tax_code_id: UUID | None
    amount: Decimal
    tax_amount: Decimal
    line_total: Decimal

    model_config = {"from_attributes": True}


class ReceiptOut(BaseModel):
    id: UUID
    company_id: UUID
    bank_account_id: UUID
    contact_id: UUID | None
    number: str | None
    receipt_date: date
    status: str
    reference: str | None
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    reason: str | None
    notes: str | None
    journal_entry_id: UUID | None
    void_journal_entry_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    lines: list[ReceiptLineOut]

    model_config = {"from_attributes": True}


class ReceiptListOut(BaseModel):
    items: list[ReceiptOut]
    total: int


def _to_out(rcpt: Receipt) -> ReceiptOut:
    return ReceiptOut.model_validate(rcpt)


def _parse_if_match(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            {"code": "if_match_required", "detail": "If-Match header required"},
        )
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "if_match_invalid", "detail": f"bad If-Match: {if_match!r}"},
        ) from exc


# ---------------------------------------------------------------------------
# POST /receipts
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ReceiptOut)
async def create_receipt(
    payload: ReceiptCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ReceiptOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        rcpt = await svc.api_create(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            actor=str(actor),
            bank_account_id=payload.bank_account_id,
            receipt_date=payload.receipt_date,
            lines=[ln.model_dump() for ln in payload.lines],
            contact_id=payload.contact_id,
            reference=payload.reference,
            reason=payload.reason,
            notes=payload.notes,
        )
    except svc.ReceiptError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "receipt_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(rcpt)


# ---------------------------------------------------------------------------
# GET /receipts
# ---------------------------------------------------------------------------


@router.get("", response_model=ReceiptListOut)
async def list_receipts(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    contact_id: UUID | None = Query(default=None),
    status_filter: ReceiptStatus | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> ReceiptListOut:
    tenant_id = resolve_tenant_id(request)
    offset = (page - 1) * page_size
    rows, total = await svc.list_active(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        contact_id=contact_id,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return ReceiptListOut(items=[_to_out(r) for r in rows], total=total)


# ---------------------------------------------------------------------------
# GET /receipts/{id}
# ---------------------------------------------------------------------------


@router.get("/{receipt_id}", response_model=ReceiptOut)
async def get_receipt(
    receipt_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ReceiptOut:
    tenant_id = resolve_tenant_id(request)
    rcpt = await svc.api_get(
        session, receipt_id, tenant_id=tenant_id, company_id=company_id
    )
    if rcpt is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "receipt_not_found", "detail": str(receipt_id)},
        )
    return _to_out(rcpt)


# ---------------------------------------------------------------------------
# PATCH /receipts/{id}
# ---------------------------------------------------------------------------


@router.patch("/{receipt_id}", response_model=ReceiptOut)
async def update_receipt(
    receipt_id: UUID,
    payload: ReceiptUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ReceiptOut:
    tenant_id = resolve_tenant_id(request)
    expected = _parse_if_match(if_match)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        rcpt = await svc.api_update(
            session,
            receipt_id,
            str(actor),
            expected,
            tenant_id=tenant_id,
            company_id=company_id,
            bank_account_id=payload.bank_account_id,
            receipt_date=payload.receipt_date,
            lines=(
                [ln.model_dump() for ln in payload.lines]
                if payload.lines is not None
                else None
            ),
            contact_id=payload.contact_id,
            reference=payload.reference,
            reason=payload.reason,
            notes=payload.notes,
        )
    except svc.VersionConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "version_conflict", "detail": str(exc)},
        ) from exc
    except svc.ReceiptError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "receipt_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(rcpt)


# ---------------------------------------------------------------------------
# POST /receipts/{id}/post
# ---------------------------------------------------------------------------


@router.post("/{receipt_id}/post", response_model=ReceiptOut)
async def post_receipt(
    receipt_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ReceiptOut:
    tenant_id = resolve_tenant_id(request)
    expected = _parse_if_match(if_match)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        rcpt = await svc.api_post(
            session,
            receipt_id,
            str(actor),
            expected,
            tenant_id=tenant_id,
            company_id=company_id,
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "version_conflict", "detail": str(exc)},
        ) from exc
    except svc.ReceiptError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "receipt_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(rcpt)


# ---------------------------------------------------------------------------
# POST /receipts/{id}/void
# ---------------------------------------------------------------------------


@router.post("/{receipt_id}/void", response_model=ReceiptOut)
async def void_receipt(
    receipt_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ReceiptOut:
    tenant_id = resolve_tenant_id(request)
    expected = _parse_if_match(if_match)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        rcpt = await svc.api_void(
            session,
            receipt_id,
            str(actor),
            expected,
            tenant_id=tenant_id,
            company_id=company_id,
        )
    except svc.VersionConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "version_conflict", "detail": str(exc)},
        ) from exc
    except svc.ReceiptError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "receipt_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(rcpt)
