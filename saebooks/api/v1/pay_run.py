"""JSON router -- /api/v1/pay-runs -- Cat-C community-tier.

No require_feature gate -- community-tier.

Endpoints
---------
POST   /api/v1/pay-runs                      -- create draft
GET    /api/v1/pay-runs                      -- list paginated
GET    /api/v1/pay-runs/{id}                 -- fetch with lines
POST   /api/v1/pay-runs/{id}/lines           -- add line
DELETE /api/v1/pay-runs/{id}/lines/{line_id} -- remove line
POST   /api/v1/pay-runs/{id}/export-aba      -- ABA + DRAFT journal
PUT    /api/v1/pay-runs/{id}/finalize        -- post journal, finalize
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    ExportAbaOut,
    PayRunConflictBody,
    PayRunCreate,
    PayRunLineCreate,
    PayRunLineOut,
    PayRunListOut,
    PayRunOut,
)
from saebooks.services import pay_runs as svc
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/pay-runs",
    tags=["pay_runs"],
    dependencies=[Depends(require_bearer)],
)


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


def _dump(pr: Any) -> dict[str, Any]:
    return json.loads(PayRunOut.model_validate(pr).model_dump_json())


# ---------------------------------------------------------------------------
# POST /pay-runs -- create draft
# ---------------------------------------------------------------------------


@router.post("", response_model=PayRunOut, status_code=201)
async def create_pay_run(
    payload: PayRunCreate,
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
                {"code": "request_in_flight", "message": "Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    try:
        pay_run = await svc.create(
            session,
            company_id,
            tenant_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            payment_date=payload.payment_date,
            description=payload.description,
            actor=f"api:{bearer[:8]}...",
        )
    except svc.PayRunError as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(pay_run)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# GET /pay-runs -- list
# ---------------------------------------------------------------------------


@router.get("", response_model=PayRunListOut)
async def list_pay_runs(
    request: Request,
    status: str | None = Query(default=None),
    period: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PayRunListOut:
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_runs(
        session,
        company_id,
        tenant_id,
        status=status,
        period=period,
        limit=limit,
        offset=offset,
    )
    return PayRunListOut(
        items=[PayRunOut.model_validate(pr) for pr in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /pay-runs/{id}
# ---------------------------------------------------------------------------


@router.get("/{pay_run_id}", response_model=PayRunOut)
async def get_pay_run(
    pay_run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> PayRunOut:
    tenant_id = resolve_tenant_id(request)
    pay_run = await svc.get(session, pay_run_id, tenant_id=tenant_id, company_id=company_id)
    if pay_run is None:
        raise HTTPException(404, "Pay run not found")
    return PayRunOut.model_validate(pay_run)


# ---------------------------------------------------------------------------
# POST /pay-runs/{id}/lines -- add line
# ---------------------------------------------------------------------------


@router.post("/{pay_run_id}/lines", response_model=PayRunLineOut, status_code=201)
async def add_line(
    pay_run_id: UUID,
    payload: PayRunLineCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: cross-company isolation (Layer 2, 2026-05-24)
    if await svc.get(session, pay_run_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Pay run not found")
    try:
        line = await svc.add_line(
            session,
            pay_run_id,
            tenant_id,
            employee_id=payload.employee_id,
            gross=payload.gross,
            tax=payload.tax,
            super_amount=payload.super_amount,
            net=payload.net,
            actor=f"api:{bearer[:8]}...",
        )
    except svc.PayRunError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(
        json.loads(PayRunLineOut.model_validate(line).model_dump_json()),
        status_code=201,
    )


# ---------------------------------------------------------------------------
# DELETE /pay-runs/{id}/lines/{line_id}
# ---------------------------------------------------------------------------


@router.delete("/{pay_run_id}/lines/{line_id}", status_code=204)
async def delete_line(
    pay_run_id: UUID,
    line_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: cross-company isolation (Layer 2, 2026-05-24)
    if await svc.get(session, pay_run_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Pay run not found")
    try:
        await svc.delete_line(
            session,
            pay_run_id,
            line_id,
            tenant_id,
            actor=f"api:{bearer[:8]}...",
        )
    except svc.PayRunError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /pay-runs/{id}/export-aba
# ---------------------------------------------------------------------------


@router.post(
    "/{pay_run_id}/export-aba",
    response_model=ExportAbaOut,
    status_code=200,
    responses={
        409: {"model": PayRunConflictBody},
        428: {"description": "If-Match required"},
    },
)
async def export_aba(
    pay_run_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with pay run version is required")

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: cross-company isolation (Layer 2, 2026-05-24)
    if await svc.get(session, pay_run_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Pay run not found")
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with different body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    try:
        aba_b64, journal_id = await svc.export_aba(
            session,
            pay_run_id,
            tenant_id,
            actor=f"api:{bearer[:8]}...",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = PayRunConflictBody(
            detail="version mismatch",
            current=PayRunOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except svc.PayRunError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = ExportAbaOut(aba_file_b64=aba_b64, journal_id=journal_id).model_dump(mode="json")
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# PUT /pay-runs/{id}/finalize
# ---------------------------------------------------------------------------


@router.put(
    "/{pay_run_id}/finalize",
    response_model=PayRunOut,
    responses={
        409: {"model": PayRunConflictBody},
        428: {"description": "If-Match required"},
    },
)
async def finalize_pay_run(
    pay_run_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with pay run version is required")

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: cross-company isolation (Layer 2, 2026-05-24)
    if await svc.get(session, pay_run_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Pay run not found")

    try:
        pay_run = await svc.finalize(
            session,
            pay_run_id,
            tenant_id,
            actor=f"api:{bearer[:8]}...",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = PayRunConflictBody(
            detail="version mismatch",
            current=PayRunOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except svc.PayRunError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(_dump(pay_run), status_code=200)
