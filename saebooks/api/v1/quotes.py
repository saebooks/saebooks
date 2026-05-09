"""JSON router — ``/api/v1/quotes``.

Pre-invoice sales document endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on PATCH / state transitions.
* Idempotency on POST via ``X-Idempotency-Key``.
* ``DELETE`` is a hard delete (admin gate per saebooks-hard-delete-policy).
* State transitions: send, accept, decline, archive, convert-to-invoice.
* RLS / tenant_id pulled from auth (resolve_tenant_id).

Auth + headers
--------------
* Bearer token required at the router level.
* ``If-Match: <version>`` required on PATCH and all state-transition POSTs.
* ``X-Idempotency-Key`` honoured on POST /.
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
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    QuoteConflictBody,
    QuoteConvertOut,
    QuoteCreate,
    QuoteListOut,
    QuoteOut,
    QuoteUpdate,
)
from saebooks.models.quote import QuoteStatus
from saebooks.services import quotes as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/quotes",
    tags=["quotes"],
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


def _dump(quote: Any) -> dict[str, Any]:
    return json.loads(QuoteOut.model_validate(quote).model_dump_json())


def _conflict_body(exc: svc.VersionConflict) -> dict[str, Any]:
    return QuoteConflictBody(
        detail="version mismatch",
        current=QuoteOut.model_validate(exc.current),
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


@router.get("", response_model=QuoteListOut)
async def list_quotes(
    request: Request,
    customer_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    since: date | None = Query(default=None),
    expiry_before: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> QuoteListOut:
    offset = (page - 1) * page_size
    status_enum: QuoteStatus | None = None
    if status is not None:
        try:
            status_enum = QuoteStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    rows, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        customer_id=customer_id,
        status=status_enum,
        since=since,
        expiry_before=expiry_before,
        limit=page_size,
        offset=offset,
    )
    return QuoteListOut(
        items=[QuoteOut.model_validate(q) for q in rows],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{quote_id}", response_model=QuoteOut)
async def get_quote(
    quote_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> QuoteOut:
    tenant_id = resolve_tenant_id(request)
    q = await svc.api_get(session, quote_id, tenant_id=tenant_id)
    if q is None:
        raise HTTPException(404, "Quote not found")
    return QuoteOut.model_validate(q)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=QuoteOut, status_code=201)
async def create_quote(
    payload: QuoteCreate,
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
        from decimal import Decimal

        q = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            customer_id=payload.customer_id,
            issue_date=payload.issue_date,
            expiry_date=payload.expiry_date,
            lines=[ln.model_dump() for ln in payload.lines],
            notes=payload.notes,
            terms=payload.terms,
            currency=payload.currency,
            validity_days=payload.validity_days,
            deposit_pct=Decimal(str(payload.deposit_pct)),
            late_fee_pct_per_month=Decimal(str(payload.late_fee_pct_per_month)),
            is_supply_only=payload.is_supply_only,
        )
    except (ValueError, svc.QuoteError) as exc:
        raise _map_value_error(exc) from exc

    body = _dump(q)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{quote_id}",
    responses={
        200: {"model": QuoteOut},
        409: {"model": QuoteConflictBody, "description": "Version mismatch"},
    },
)
async def update_quote(
    quote_id: UUID,
    payload: QuoteUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with quote version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, quote_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Quote not found")

    try:
        from decimal import Decimal

        lines_data = (
            [ln.model_dump() for ln in payload.lines]
            if payload.lines is not None
            else None
        )
        q = await svc.api_update(
            session,
            quote_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            customer_id=payload.customer_id,
            issue_date=payload.issue_date,
            expiry_date=payload.expiry_date,
            notes=payload.notes,
            terms=payload.terms,
            currency=payload.currency,
            validity_days=payload.validity_days,
            deposit_pct=(
                Decimal(str(payload.deposit_pct))
                if payload.deposit_pct is not None
                else None
            ),
            late_fee_pct_per_month=(
                Decimal(str(payload.late_fee_pct_per_month))
                if payload.late_fee_pct_per_month is not None
                else None
            ),
            is_supply_only=payload.is_supply_only,
            lines=lines_data,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        return JSONResponse(_conflict_body(exc), status_code=409)
    except (ValueError, svc.QuoteError) as exc:
        raise _map_value_error(exc) from exc

    return JSONResponse(_dump(q), status_code=200)


# ---------------------------------------------------------------------------
# Hard delete (DELETE → 204, admin gate)
# ---------------------------------------------------------------------------


@router.delete(
    "/{quote_id}",
    responses={
        204: {"description": "Deleted"},
    },
)
async def delete_quote(
    quote_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, quote_id, tenant_id=tenant_id)
    if existing is None:
        raise HTTPException(404, "Quote not found")

    # Quotes use hard delete per saebooks-hard-delete-policy.
    # When the admin gate is active (hard=True) or when no gate header
    # is supplied (default behaviour for quotes — no soft-archive column).
    await hard_delete_with_audit(
        session, existing, "quotes", getattr(request.state, "user", None)
    )
    await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# State transitions — send / accept / decline / archive
# ---------------------------------------------------------------------------


def _idempotent_transition_factory(action: str):
    """Returns a route handler that runs ``svc.api_<action>``
    with idempotency-key + If-Match handling."""

    async def handler(
        quote_id: UUID,
        request: Request,
        if_match: str | None = Header(default=None, alias="If-Match"),
        idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
        bearer: str = Depends(require_bearer),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        expected = _parse_if_match(if_match)
        if expected is None:
            raise HTTPException(428, "If-Match header with quote version is required")

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

        if await svc.api_get(session, quote_id, tenant_id=tenant_id) is None:
            raise HTTPException(404, "Quote not found")

        method = getattr(svc, f"api_{action}")
        try:
            q = await method(
                session,
                quote_id,
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
        except (ValueError, svc.QuoteError) as exc:
            raise _map_value_error(exc) from exc

        body = _dump(q)
        if key is not None:
            await store_response(session, key, 200, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=200)

    return handler


router.add_api_route(
    "/{quote_id}/send",
    _idempotent_transition_factory("send"),
    methods=["POST"],
    responses={
        200: {"model": QuoteOut},
        409: {"model": QuoteConflictBody, "description": "Version mismatch"},
    },
)
router.add_api_route(
    "/{quote_id}/accept",
    _idempotent_transition_factory("accept"),
    methods=["POST"],
    responses={
        200: {"model": QuoteOut},
        409: {"model": QuoteConflictBody, "description": "Version mismatch"},
    },
)
router.add_api_route(
    "/{quote_id}/decline",
    _idempotent_transition_factory("decline"),
    methods=["POST"],
    responses={
        200: {"model": QuoteOut},
        409: {"model": QuoteConflictBody, "description": "Version mismatch"},
    },
)
router.add_api_route(
    "/{quote_id}/archive",
    _idempotent_transition_factory("archive"),
    methods=["POST"],
    responses={
        200: {"model": QuoteOut},
        409: {"model": QuoteConflictBody, "description": "Version mismatch"},
    },
)


# ---------------------------------------------------------------------------
# Convert-to-invoice (ACCEPTED → INVOICED, returns invoice_id)
# ---------------------------------------------------------------------------


@router.post(
    "/{quote_id}/convert-to-invoice",
    responses={
        200: {"model": QuoteConvertOut},
        409: {"model": QuoteConflictBody, "description": "Version mismatch"},
    },
)
async def convert_to_invoice(
    quote_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with quote version is required")

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

    if await svc.api_get(session, quote_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Quote not found")

    try:
        q, inv = await svc.convert_to_invoice(
            session,
            quote_id,
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
    except (ValueError, svc.QuoteError) as exc:
        raise _map_value_error(exc) from exc

    body = QuoteConvertOut(
        quote=QuoteOut.model_validate(q),
        invoice_id=inv.id,
    ).model_dump(mode="json")
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)
