"""JSON router — ``/api/v1/supplier_credit_notes``.

The purchase-side mirror of ``/api/v1/credit_notes``. A supplier (purchase)
credit note reverses a purchase: Dr AP control, Cr expense, Cr GST Paid (input
credit reversed). See ``services/supplier_credit_notes.py``.

Endpoints (mirror the credit_notes / payments surface):

* ``POST   /supplier_credit_notes``               — create a draft.
* ``GET    /supplier_credit_notes``               — list (filter by status).
* ``GET    /supplier_credit_notes/{id}``          — single.
* ``PATCH  /supplier_credit_notes/{id}``          — edit a draft (If-Match).
* ``POST   /supplier_credit_notes/{id}/post``     — DRAFT -> POSTED (If-Match).
* ``POST   /supplier_credit_notes/{id}/void``     — POSTED -> VOIDED (If-Match).

Auth: standard Bearer. ``tenant_id`` from JWT claims; ``company_id`` from
``X-Company-Id`` (or first active company).
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
from saebooks.models.supplier_credit_note import (
    SupplierCreditNote,
    SupplierCreditNoteStatus,
)
from saebooks.services import supplier_credit_notes as svc

router = APIRouter(
    prefix="/supplier_credit_notes",
    tags=["supplier_credit_notes"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SCNLineIn(BaseModel):
    description: str = Field(min_length=1)
    account_id: UUID
    tax_code_id: UUID | None = None
    quantity: Decimal = Field(default=Decimal("1"))
    unit_price: Decimal = Field(default=Decimal("0"))
    discount_pct: Decimal = Field(default=Decimal("0"))


class SCNCreate(BaseModel):
    contact_id: UUID
    issue_date: date
    lines: list[SCNLineIn] = Field(default_factory=list)
    original_bill_id: UUID | None = None
    supplier_reference: str | None = Field(default=None, max_length=255)
    reason: str | None = None
    notes: str | None = None


class SCNUpdate(BaseModel):
    contact_id: UUID | None = None
    issue_date: date | None = None
    lines: list[SCNLineIn] | None = None
    original_bill_id: UUID | None = None
    supplier_reference: str | None = Field(default=None, max_length=255)
    reason: str | None = None
    notes: str | None = None


class SCNLineOut(BaseModel):
    id: UUID
    line_no: int
    description: str
    account_id: UUID
    tax_code_id: UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal

    model_config = {"from_attributes": True}


class SCNOut(BaseModel):
    id: UUID
    company_id: UUID
    contact_id: UUID
    number: str | None
    issue_date: date
    status: str
    original_bill_id: UUID | None
    supplier_reference: str | None
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
    lines: list[SCNLineOut]

    model_config = {"from_attributes": True}


class SCNListOut(BaseModel):
    items: list[SCNOut]
    total: int


def _to_out(scn: SupplierCreditNote) -> SCNOut:
    return SCNOut.model_validate(scn)


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
# POST /supplier_credit_notes
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SCNOut)
async def create_scn(
    payload: SCNCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> SCNOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        scn = await svc.api_create(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            actor=str(actor),
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            lines=[ln.model_dump() for ln in payload.lines],
            original_bill_id=payload.original_bill_id,
            supplier_reference=payload.supplier_reference,
            reason=payload.reason,
            notes=payload.notes,
        )
    except svc.SupplierCreditNoteError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "supplier_credit_note_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(scn)


# ---------------------------------------------------------------------------
# GET /supplier_credit_notes
# ---------------------------------------------------------------------------


@router.get("", response_model=SCNListOut)
async def list_scn(
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    contact_id: UUID | None = Query(default=None),
    status_filter: SupplierCreditNoteStatus | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> SCNListOut:
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
    return SCNListOut(items=[_to_out(r) for r in rows], total=total)


# ---------------------------------------------------------------------------
# GET /supplier_credit_notes/{id}
# ---------------------------------------------------------------------------


@router.get("/{scn_id}", response_model=SCNOut)
async def get_scn(
    scn_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> SCNOut:
    tenant_id = resolve_tenant_id(request)
    scn = await svc.api_get(
        session, scn_id, tenant_id=tenant_id, company_id=company_id
    )
    if scn is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "supplier_credit_note_not_found", "detail": str(scn_id)},
        )
    return _to_out(scn)


# ---------------------------------------------------------------------------
# PATCH /supplier_credit_notes/{id}
# ---------------------------------------------------------------------------


@router.patch("/{scn_id}", response_model=SCNOut)
async def update_scn(
    scn_id: UUID,
    payload: SCNUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> SCNOut:
    tenant_id = resolve_tenant_id(request)
    expected = _parse_if_match(if_match)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        scn = await svc.api_update(
            session,
            scn_id,
            str(actor),
            expected,
            tenant_id=tenant_id,
            company_id=company_id,
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            lines=(
                [ln.model_dump() for ln in payload.lines]
                if payload.lines is not None
                else None
            ),
            original_bill_id=payload.original_bill_id,
            supplier_reference=payload.supplier_reference,
            reason=payload.reason,
            notes=payload.notes,
        )
    except svc.VersionConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "version_conflict", "detail": str(exc)},
        ) from exc
    except svc.SupplierCreditNoteError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "supplier_credit_note_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(scn)


# ---------------------------------------------------------------------------
# POST /supplier_credit_notes/{id}/post
# ---------------------------------------------------------------------------


@router.post("/{scn_id}/post", response_model=SCNOut)
async def post_scn(
    scn_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> SCNOut:
    tenant_id = resolve_tenant_id(request)
    expected = _parse_if_match(if_match)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        scn = await svc.api_post(
            session,
            scn_id,
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
    except svc.SupplierCreditNoteError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "supplier_credit_note_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(scn)


# ---------------------------------------------------------------------------
# POST /supplier_credit_notes/{id}/void
# ---------------------------------------------------------------------------


@router.post("/{scn_id}/void", response_model=SCNOut)
async def void_scn(
    scn_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> SCNOut:
    tenant_id = resolve_tenant_id(request)
    expected = _parse_if_match(if_match)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        scn = await svc.api_void(
            session,
            scn_id,
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
    except svc.SupplierCreditNoteError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "supplier_credit_note_invalid", "detail": str(exc)},
        ) from exc
    return _to_out(scn)
