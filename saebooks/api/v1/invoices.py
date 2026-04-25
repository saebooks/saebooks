"""JSON router — ``/api/v1/invoices``.

Phase 1 tier-3 accounts-receivable endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Lines are nested in the response.
* B/48: ``POST /{id}/stripe-payment-link`` generates a Stripe Checkout
  Session URL (gated on ``FLAG_STRIPE_INTEGRATION``).

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; all queries within the request are gated by the
``tenant_isolation`` RLS policy from migration 0055. ``_first_company_id``
is scoped by the request tenant.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    InvoiceConflictBody,
    InvoiceCreate,
    InvoiceListOut,
    InvoiceOut,
    InvoiceUpdate,
)
from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.models.invoice import InvoiceStatus
from saebooks.services import invoices as svc
from saebooks.services.features import FLAG_STRIPE_INTEGRATION, require_feature
from saebooks.services.integrations import StripeError, StripeNotConfiguredError
from saebooks.services.integrations.stripe_links import create_payment_link

router = APIRouter(
    prefix="/invoices",
    tags=["invoices"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession, tenant_id: UUID) -> UUID:
    """Return the first active company for the request tenant."""
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "No active company for tenant")
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
    request: Request,
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> InvoiceListOut:
    offset = (page - 1) * page_size
    status_enum: InvoiceStatus | None = None
    if status is not None:
        try:
            status_enum = InvoiceStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
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
async def get_invoice(
    invoice_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> InvoiceOut:
    tenant_id = resolve_tenant_id(request)
    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return InvoiceOut.model_validate(inv)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    payload: InvoiceCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
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
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    tenant_id = resolve_tenant_id(request)
    # Confirm the invoice belongs to the caller's tenant before touching it.
    if await svc.api_get(session, invoice_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Invoice not found")

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
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, invoice_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Invoice not found")

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


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/post",
    responses={
        200: {"model": InvoiceOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def post_invoice(
    invoice_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Transition invoice DRAFT → POSTED, generating journal entry lines."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, invoice_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        inv = await svc.api_post_invoice(
            session,
            invoice_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
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
# Void via status transition (POST /{id}/void → VOIDED with JE reversal)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/void",
    responses={
        200: {"model": InvoiceOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def void_invoice_transition(
    invoice_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Transition any non-VOIDED invoice → VOIDED, reversing JE if POSTED."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, invoice_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        inv = await svc.api_void_invoice(
            session,
            invoice_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
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
# Stripe payment link (B/48 — POST /{id}/stripe-payment-link)
# Gated on FLAG_STRIPE_INTEGRATION (Business+).
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/stripe-payment-link",
    dependencies=[Depends(require_feature(FLAG_STRIPE_INTEGRATION))],
)
async def create_stripe_payment_link(
    invoice_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Generate a Stripe Checkout Session URL for a posted invoice."""
    tenant_id = resolve_tenant_id(request)
    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")

    if inv.status != InvoiceStatus.POSTED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Invoice must be POSTED to generate a payment link "
            f"(current status: {inv.status.value})",
        )

    balance_due = inv.total - inv.amount_paid
    if balance_due <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Invoice has no outstanding balance; payment link not generated",
        )

    invoice_dict: dict[str, Any] = {
        "id": str(inv.id),
        "number": inv.number,
        "currency": inv.currency,
        "total": inv.total,
        "lines": [
            {
                "description": line.description,
                "line_total": line.line_total,
            }
            for line in inv.lines
        ],
    }

    try:
        url = await create_payment_link(invoice_dict, settings=settings)
    except StripeNotConfiguredError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Stripe is not configured: {exc}",
        ) from exc
    except StripeError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Stripe API error: {exc}",
        ) from exc

    inv.stripe_payment_link = url
    await session.commit()

    return JSONResponse({"payment_link": url})
