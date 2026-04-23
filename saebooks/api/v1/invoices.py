"""JSON router — ``/api/v1/invoices``.

Phase 1 tier-3 accounts-receivable endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Lines are nested in the response.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    InvoiceConflictBody,
    InvoiceCreate,
    InvoiceListOut,
    InvoiceOut,
    InvoiceUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.invoice import InvoiceStatus
from saebooks.services import invoices as svc

router = APIRouter(
    prefix="/invoices",
    tags=["invoices"],
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


def _dump(inv: Any) -> dict[str, Any]:
    return json.loads(InvoiceOut.model_validate(inv).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=InvoiceListOut)
async def list_invoices(
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> InvoiceListOut:
    offset = (page - 1) * page_size
    status_enum: InvoiceStatus | None = None
    if status is not None:
        try:
            status_enum = InvoiceStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        invoices, total = await svc.list_active(
            session,
            company_id,
            tenant_id,
            contact_id=contact_id,
            status=status_enum,
            date_from=date_from,
            date_to=date_to,
            limit=page_size,
            offset=offset,
        )
        return InvoiceListOut(
            items=[InvoiceOut.model_validate(inv) for inv in invoices],
            total=total,
            limit=page_size,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: UUID) -> InvoiceOut:
    async with AsyncSessionLocal() as session:
        inv = await svc.api_get(session, invoice_id)
        if inv is None:
            raise HTTPException(404, "Invoice not found")
        return InvoiceOut.model_validate(inv)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    payload: InvoiceCreate,
    bearer: str = Depends(require_bearer),
) -> Any:
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        try:
            inv = await svc.api_create(
                session,
                company_id,
                tenant_id,
                actor=f"api:{bearer[:8]}…",
                contact_id=payload.contact_id,
                issue_date=payload.issue_date,
                due_date=payload.due_date,
                lines=[line.model_dump() for line in payload.lines],
                notes=payload.notes,
                payment_terms=payload.payment_terms,
                currency=payload.currency,
            )
        except (ValueError, svc.InvoiceError) as exc:
            raise HTTPException(422, str(exc)) from exc

        body = _dump(inv)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{invoice_id}",
    responses={
        200: {"model": InvoiceOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def update_invoice(
    invoice_id: UUID,
    payload: InvoiceUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    async with AsyncSessionLocal() as session:
        try:
            lines_data = (
                [line.model_dump() for line in payload.lines]
                if payload.lines is not None
                else None
            )
            inv = await svc.api_update(
                session,
                invoice_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                contact_id=payload.contact_id,
                issue_date=payload.issue_date,
                due_date=payload.due_date,
                notes=payload.notes,
                payment_terms=payload.payment_terms,
                lines=lines_data,
            )
        except svc.VersionConflict as exc:
            body = InvoiceConflictBody(
                detail="version mismatch",
                current=InvoiceOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.InvoiceError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(inv)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{invoice_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def void_invoice(
    invoice_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    async with AsyncSessionLocal() as session:
        try:
            await svc.api_void(
                session,
                invoice_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        except svc.VersionConflict as exc:
            body = InvoiceConflictBody(
                detail="version mismatch",
                current=InvoiceOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.InvoiceError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

    return Response(status_code=204)
