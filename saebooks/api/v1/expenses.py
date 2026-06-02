"""JSON router — ``/api/v1/expenses``.

Paid-at-checkout sibling of ``/api/v1/bills``. Same auth / locking /
idempotency / soft-delete pattern; differs only in that there is no
AP step — the expense's ``payment_account_id`` is credited directly on
post, and there's no separate Payment row to allocate against later.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete/post/void.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at) returning 204.
* Lines are nested in the response.
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
from saebooks.api.v1.edit_force_gate import edit_force_admin_gate
from saebooks.api.v1.schemas import (
    ExpenseConflictBody,
    ExpenseCreate,
    ExpenseListOut,
    ExpenseOut,
    ExpenseUpdate,
)
from saebooks.models.expense import ExpenseStatus
from saebooks.services import expenses as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/expenses",
    tags=["expenses"],
    dependencies=[Depends(require_bearer)],
)


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(expense: Any) -> dict[str, Any]:
    return json.loads(ExpenseOut.model_validate(expense).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ExpenseListOut)
async def list_expenses(
    request: Request,
    contact_id: UUID | None = Query(default=None),
    payment_account_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ExpenseListOut:
    offset = (page - 1) * page_size
    status_enum: ExpenseStatus | None = None
    if status is not None:
        try:
            status_enum = ExpenseStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    expenses, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        contact_id=contact_id,
        payment_account_id=payment_account_id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return ExpenseListOut(
        items=[ExpenseOut.model_validate(e) for e in expenses],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{expense_id}", response_model=ExpenseOut)
async def get_expense(
    expense_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ExpenseOut:
    tenant_id = resolve_tenant_id(request)
    expense = await svc.api_get(session, expense_id, tenant_id=tenant_id, company_id=company_id)
    if expense is None:
        raise HTTPException(404, "Expense not found")
    return ExpenseOut.model_validate(expense)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=ExpenseOut, status_code=201)
async def create_expense(
    payload: ExpenseCreate,
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
        expense = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            payment_account_id=payload.payment_account_id,
            expense_date=payload.expense_date,
            contact_id=payload.contact_id,
            lines=[line.model_dump() for line in payload.lines],
            reference=payload.reference,
            notes=payload.notes,
            currency=payload.currency,
            fx_rate=payload.fx_rate,
        )
    except (ValueError, svc.ExpenseError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(expense)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{expense_id}",
    responses={
        200: {"model": ExpenseOut},
        409: {"model": ExpenseConflictBody, "description": "Version mismatch"},
    },
)
async def update_expense(
    expense_id: UUID,
    payload: ExpenseUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with expense version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, expense_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Expense not found")

    try:
        lines_data = (
            [line.model_dump() for line in payload.lines]
            if payload.lines is not None
            else None
        )
        expense = await svc.api_update(
            session,
            expense_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            payment_account_id=payload.payment_account_id,
            contact_id=payload.contact_id,
            expense_date=payload.expense_date,
            notes=payload.notes,
            reference=payload.reference,
            currency=payload.currency,
            fx_rate=payload.fx_rate,
            lines=lines_data,
        )
    except svc.VersionConflict as exc:
        body = ExpenseConflictBody(
            detail="version mismatch",
            current=ExpenseOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.ExpenseError) as exc:
        msg = str(exc)
        if "not found in current tenant" in msg.lower():
            raise HTTPException(422, msg) from exc
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(expense)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Archive / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{expense_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": ExpenseConflictBody, "description": "Version mismatch"},
    },
)
async def archive_expense(
    expense_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, expense_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Expense not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "expenses", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with expense version is required")

    try:
        await svc.api_archive(
            session,
            expense_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = ExpenseConflictBody(
            detail="version mismatch",
            current=ExpenseOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.ExpenseError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{expense_id}/post",
    responses={
        200: {"model": ExpenseOut},
        409: {"model": ExpenseConflictBody, "description": "Version mismatch"},
    },
)
async def post_expense(
    expense_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with expense version is required")

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

    if await svc.api_get(session, expense_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Expense not found")

    try:
        expense = await svc.api_post_expense(
            session,
            expense_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        body = ExpenseConflictBody(
            detail="version mismatch",
            current=ExpenseOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.ExpenseError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(expense)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void transition (POST /{id}/void → VOIDED with JE reversal)
# ---------------------------------------------------------------------------


@router.post(
    "/{expense_id}/void",
    responses={
        200: {"model": ExpenseOut},
        409: {"model": ExpenseConflictBody, "description": "Version mismatch"},
    },
)
async def void_expense_transition(
    expense_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with expense version is required")

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

    if await svc.api_get(session, expense_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Expense not found")

    try:
        expense = await svc.api_void_expense(
            session,
            expense_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        body = ExpenseConflictBody(
            detail="version mismatch",
            current=ExpenseOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.ExpenseError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(expense)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)
