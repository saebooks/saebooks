"""JSON router — ``/api/v1/purchase_orders``.

Surface
-------
* ``GET    /``                      — paginated list, filterable by
                                       contact / status / date range.
* ``GET    /{po_id}``               — fetch one with lines.
* ``POST   /``                      — create DRAFT (idempotency-key safe).
* ``PATCH  /{po_id}``               — update DRAFT or OPEN/PARTIAL with
                                       optimistic locking (``If-Match``).
* ``POST   /{po_id}/send``          — DRAFT → OPEN; mints PO number.
* ``POST   /{po_id}/cancel``        — non-terminal → CANCELLED.
* ``POST   /{po_id}/close``         — OPEN/PARTIAL/RECEIVED → CLOSED.
* ``POST   /{po_id}/convert-to-bill``
                                    — emit a DRAFT bill from the
                                       outstanding lines, advance
                                       ``received_qty``, auto-flip
                                       status (PARTIAL or RECEIVED).
* ``DELETE /{po_id}``               — soft-archive (or hard-delete with
                                       admin gate, mirrors bills).

Auth + headers (mirrors ``bills``)
----------------------------------
* Bearer token required at the router level.
* ``If-Match: <version>`` is required on all mutating routes that need
  optimistic-locking (PATCH, send, cancel, close, convert-to-bill,
  DELETE).
* ``X-Idempotency-Key`` is honoured on POST / and the state-transition
  POSTs.
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
from saebooks.api.v1.edit_force_gate import edit_force_admin_gate
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    PurchaseOrderConflictBody,
    PurchaseOrderConvertBody,
    PurchaseOrderConvertOut,
    PurchaseOrderCreate,
    PurchaseOrderListOut,
    PurchaseOrderOut,
    PurchaseOrderUpdate,
)
from saebooks.models.purchase_order import PurchaseOrderStatus
from saebooks.services import purchase_orders as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/purchase_orders",
    tags=["purchase_orders"],
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
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(po: Any) -> dict[str, Any]:
    return json.loads(PurchaseOrderOut.model_validate(po).model_dump_json())


def _conflict_body(exc: svc.VersionConflict) -> dict[str, Any]:
    return PurchaseOrderConflictBody(
        detail="version mismatch",
        current=PurchaseOrderOut.model_validate(exc.current),
    ).model_dump(mode="json")


def _map_value_error(exc: Exception) -> HTTPException:
    msg = str(exc)
    if "not found in current tenant" in msg.lower():
        return HTTPException(422, msg)
    if "not found" in msg.lower():
        return HTTPException(404, msg)
    return HTTPException(422, msg)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=PurchaseOrderListOut)
async def list_purchase_orders(
    request: Request,
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PurchaseOrderListOut:
    offset = (page - 1) * page_size
    status_enum: PurchaseOrderStatus | None = None
    if status is not None:
        try:
            status_enum = PurchaseOrderStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    rows, total = await svc.list_active(
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
    return PurchaseOrderListOut(
        items=[PurchaseOrderOut.model_validate(po) for po in rows],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{po_id}", response_model=PurchaseOrderOut)
async def get_purchase_order(
    po_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PurchaseOrderOut:
    tenant_id = resolve_tenant_id(request)
    po = await svc.api_get(session, po_id, tenant_id=tenant_id, company_id=company_id)
    if po is None:
        raise HTTPException(404, "PurchaseOrder not found")
    return PurchaseOrderOut.model_validate(po)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=PurchaseOrderOut, status_code=201)
async def create_purchase_order(
    payload: PurchaseOrderCreate,
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
                {
                    "code": "idempotency_key_conflict",
                    "message": "X-Idempotency-Key reused with a different request body",
                },
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {
                    "code": "request_in_flight",
                    "message": "A request with this idempotency key is currently being processed. Retry after 1 second.",
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    try:
        po = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            expected_date=payload.expected_date,
            delivery_address=payload.delivery_address,
            lines=[ln.model_dump() for ln in payload.lines],
            notes=payload.notes,
            currency=payload.currency,
            fx_rate=payload.fx_rate,
        )
    except (ValueError, svc.PurchaseOrderError) as exc:
        raise _map_value_error(exc) from exc

    body = _dump(po)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{po_id}",
    responses={
        200: {"model": PurchaseOrderOut},
        409: {"model": PurchaseOrderConflictBody, "description": "Version mismatch"},
    },
)
async def update_purchase_order(
    po_id: UUID,
    payload: PurchaseOrderUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with PO version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, po_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "PurchaseOrder not found")

    try:
        lines_data = (
            [ln.model_dump() for ln in payload.lines]
            if payload.lines is not None
            else None
        )
        po = await svc.api_update(
            session,
            po_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            expected_date=payload.expected_date,
            delivery_address=payload.delivery_address,
            notes=payload.notes,
            currency=payload.currency,
            fx_rate=payload.fx_rate,
            lines=lines_data,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        return JSONResponse(_conflict_body(exc), status_code=409)
    except (ValueError, svc.PurchaseOrderError) as exc:
        raise _map_value_error(exc) from exc

    return JSONResponse(_dump(po), status_code=200)


# ---------------------------------------------------------------------------
# Send / cancel / close transitions
# ---------------------------------------------------------------------------


def _idempotent_state_transition_factory(action: str):
    """Returns a route handler closure that runs ``svc.api_<action>``
    with idempotency-key + If-Match handling."""

    async def handler(
        po_id: UUID,
        request: Request,
        if_match: str | None = Header(default=None, alias="If-Match"),
        idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
        bearer: str = Depends(require_bearer),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        expected = _parse_if_match(if_match)
        if expected is None:
            raise HTTPException(428, "If-Match header with PO version is required")

        tenant_id = resolve_tenant_id(request)
        key = _parse_idempotency_key(idempotency_key)

        if key is not None:
            raw_body = await request.body()
            body_sha256 = hashlib.sha256(raw_body).hexdigest()
            claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
            if claim.status == ClaimStatus.CONFLICT:
                return JSONResponse(
                    {
                        "code": "idempotency_key_conflict",
                        "message": "X-Idempotency-Key reused with a different request body",
                    },
                    status_code=422,
                )
            if claim.status == ClaimStatus.IN_FLIGHT:
                return JSONResponse(
                    {
                        "code": "request_in_flight",
                        "message": "A request with this idempotency key is currently being processed. Retry after 1 second.",
                    },
                    status_code=503,
                    headers={"Retry-After": "1"},
                )
            if claim.status == ClaimStatus.REPLAY:
                return JSONResponse(
                    content=json.loads(claim.response_body) if claim.response_body else {},
                    status_code=claim.response_status or 200,
                )

        if await svc.api_get(session, po_id, tenant_id=tenant_id) is None:
            raise HTTPException(404, "PurchaseOrder not found")

        method = getattr(svc, f"api_{action}")
        try:
            po = await method(
                session,
                po_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                tenant_id=tenant_id,
            )
        except svc.VersionConflict as exc:
            body = _conflict_body(exc)
            if key is not None:
                await store_response(session, key, 409, json.dumps(body).encode())
                await session.commit()
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.PurchaseOrderError) as exc:
            raise _map_value_error(exc) from exc

        body = _dump(po)
        if key is not None:
            await store_response(session, key, 200, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=200)

    return handler


router.add_api_route(
    "/{po_id}/send",
    _idempotent_state_transition_factory("send"),
    methods=["POST"],
    responses={
        200: {"model": PurchaseOrderOut},
        409: {"model": PurchaseOrderConflictBody, "description": "Version mismatch"},
    },
)
router.add_api_route(
    "/{po_id}/cancel",
    _idempotent_state_transition_factory("cancel"),
    methods=["POST"],
    responses={
        200: {"model": PurchaseOrderOut},
        409: {"model": PurchaseOrderConflictBody, "description": "Version mismatch"},
    },
)
router.add_api_route(
    "/{po_id}/close",
    _idempotent_state_transition_factory("close"),
    methods=["POST"],
    responses={
        200: {"model": PurchaseOrderOut},
        409: {"model": PurchaseOrderConflictBody, "description": "Version mismatch"},
    },
)


# ---------------------------------------------------------------------------
# Convert-to-bill (custom — needs a body and returns the new bill id)
# ---------------------------------------------------------------------------


@router.post(
    "/{po_id}/convert-to-bill",
    responses={
        200: {"model": PurchaseOrderConvertOut},
        409: {"model": PurchaseOrderConflictBody, "description": "Version mismatch"},
    },
)
async def convert_to_bill(
    po_id: UUID,
    payload: PurchaseOrderConvertBody,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with PO version is required")

    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {
                    "code": "idempotency_key_conflict",
                    "message": "X-Idempotency-Key reused with a different request body",
                },
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {
                    "code": "request_in_flight",
                    "message": "A request with this idempotency key is currently being processed. Retry after 1 second.",
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    if await svc.api_get(session, po_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "PurchaseOrder not found")

    try:
        po, bill = await svc.convert_to_bill(
            session,
            po_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            quantities=payload.quantities,
            bill_issue_date=payload.bill_issue_date,
            bill_due_date=payload.bill_due_date,
            supplier_reference=payload.supplier_reference,
        )
    except svc.VersionConflict as exc:
        body = _conflict_body(exc)
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.PurchaseOrderError) as exc:
        raise _map_value_error(exc) from exc

    body = PurchaseOrderConvertOut(
        purchase_order=PurchaseOrderOut.model_validate(po),
        bill_id=bill.id,
        bill_number=bill.number,
    ).model_dump(mode="json")
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Archive / hard-delete (DELETE → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{po_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": PurchaseOrderConflictBody, "description": "Version mismatch"},
    },
)
async def archive_purchase_order(
    po_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, po_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "PurchaseOrder not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "purchase_orders", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with PO version is required")

    try:
        await svc.api_archive(
            session,
            po_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        return JSONResponse(_conflict_body(exc), status_code=409)
    except (ValueError, svc.PurchaseOrderError) as exc:
        raise _map_value_error(exc) from exc

    return Response(status_code=204)
