"""JSON router — ``/api/v1/payments``.

Phase 1 tier-3 accounts-receivable/payable payments endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Allocations are nested in the response.

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; queries are gated by the ``tenant_isolation`` RLS
policy from migration 0055.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    PaymentConflictBody,
    PaymentCreate,
    PaymentListOut,
    PaymentOut,
    PaymentUpdate,
)
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.edit_force_gate import edit_force_admin_gate
from saebooks.models.payment import PaymentDirection, PaymentMethod
from saebooks.services import payments as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.journal import PostingError
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/payments",
    tags=["payments"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(payment: Any) -> dict[str, Any]:
    return json.loads(PaymentOut.model_validate(payment).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=PaymentListOut)
async def list_payments(
    request: Request,
    contact_id: UUID | None = Query(default=None),
    direction: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PaymentListOut:
    offset = (page - 1) * page_size
    direction_enum: PaymentDirection | None = None
    if direction is not None:
        try:
            direction_enum = PaymentDirection(direction.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid direction '{direction}'") from exc

    tenant_id = resolve_tenant_id(request)
    payments, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        contact_id=contact_id,
        direction=direction_enum,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return PaymentListOut(
        items=[PaymentOut.model_validate(p) for p in payments],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PaymentOut:
    tenant_id = resolve_tenant_id(request)
    payment = await svc.api_get(session, payment_id, tenant_id=tenant_id, company_id=company_id)
    if payment is None:
        raise HTTPException(404, "Payment not found")
    return PaymentOut.model_validate(payment)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=PaymentOut, status_code=201)
async def create_payment(
    payload: PaymentCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    try:
        direction_enum = PaymentDirection(payload.direction.upper())
    except ValueError as exc:
        raise HTTPException(400, f"Invalid direction '{payload.direction}'") from exc
    try:
        method_enum = PaymentMethod(payload.method.lower())
    except ValueError as exc:
        raise HTTPException(400, f"Invalid method '{payload.method}'") from exc

    try:
        payment = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            contact_id=payload.contact_id,
            bank_account_id=payload.bank_account_id,
            payment_date=payload.payment_date,
            amount=payload.amount,
            direction=direction_enum,
            method=method_enum,
            reference=payload.reference,
            notes=payload.notes,
            currency=payload.currency,
            allocations=[a.model_dump() for a in payload.allocations],
        )
    except (ValueError, svc.PaymentError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(payment)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{payment_id}",
    responses={
        200: {"model": PaymentOut},
        409: {"model": PaymentConflictBody, "description": "Version mismatch"},
    },
)
async def update_payment(
    payment_id: UUID,
    payload: PaymentUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with payment version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, payment_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Payment not found")

    direction_enum: PaymentDirection | None = None
    if payload.direction is not None:
        try:
            direction_enum = PaymentDirection(payload.direction.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid direction '{payload.direction}'") from exc

    method_enum: PaymentMethod | None = None
    if payload.method is not None:
        try:
            method_enum = PaymentMethod(payload.method.lower())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid method '{payload.method}'") from exc

    try:
        allocs_data = (
            [a.model_dump() for a in payload.allocations]
            if payload.allocations is not None
            else None
        )
        payment = await svc.api_update(
            session,
            payment_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            contact_id=payload.contact_id,
            bank_account_id=payload.bank_account_id,
            payment_date=payload.payment_date,
            amount=payload.amount,
            direction=direction_enum,
            method=method_enum,
            reference=payload.reference,
            notes=payload.notes,
            currency=payload.currency,
            allocations=allocs_data,
        )
    except svc.VersionConflict as exc:
        body = PaymentConflictBody(
            detail="version mismatch",
            current=PaymentOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.PaymentError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(payment)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{payment_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": PaymentConflictBody, "description": "Version mismatch"},
    },
)
async def void_payment(
    payment_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, payment_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Payment not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "payments", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with payment version is required")

    try:
        await svc.api_void(
            session,
            payment_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = PaymentConflictBody(
            detail="version mismatch",
            current=PaymentOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.PaymentError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Post transition (POST /{payment_id}/post → DRAFT → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{payment_id}/post",
    responses={
        200: {"model": PaymentOut},
        409: {"model": PaymentConflictBody, "description": "Version mismatch"},
    },
)
async def post_payment_endpoint(
    payment_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition payment DRAFT → POSTED, generating the GL journal entry.

    Symmetric with POST /api/v1/invoices/{id}/post and
    POST /api/v1/bills/{id}/post.

    * Requires If-Match: <version> for optimistic locking.
    * Supports X-Idempotency-Key for at-most-once semantics.
    * 200 → posted payment body.
    * 409 → version mismatch (current state in body).
    * 422 → payment already posted/voided, or PostingError from the
        journal layer (e.g. cross-company account reference).
    * 404 → payment not found.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with payment version is required")

    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    if await svc.api_get(session, payment_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Payment not found")

    try:
        payment = await svc.api_post_payment(
            session,
            payment_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        body = PaymentConflictBody(
            detail="version mismatch",
            current=PaymentOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except PostingError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, svc.PaymentError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(payment)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)
