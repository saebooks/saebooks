"""JSON router — ``/api/v1/budgets``.

Phase 1 tier-4 budget endpoint.

Budgets are flat monthly-amount-per-account rows — they are a reporting
overlay and never touch the GL. The unique key is
``(company_id, account_id, year, month)``.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
* No line-item replace semantics (rows are atomic).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    BudgetConflictBody,
    BudgetCreate,
    BudgetListOut,
    BudgetOut,
    BudgetUpdate,
)
from saebooks.services import budgets as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/budgets",
    tags=["budgets"],
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
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(b: Any) -> dict[str, Any]:
    return json.loads(BudgetOut.model_validate(b).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=BudgetListOut)
async def list_budgets(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    account_id: str | None = Query(default=None),
    archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BudgetListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_budgets(
        session,
        company_id,
        tenant_id,
        year=year,
        month=month,
        account_id=account_id,
        archived=archived,
        limit=page_size,
        offset=offset,
    )
    return BudgetListOut(
        items=[BudgetOut.model_validate(b) for b in items],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{budget_id}", response_model=BudgetOut)
async def get_budget(
    request: Request,
    budget_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BudgetOut:
    tenant_id = resolve_tenant_id(request)
    b = await svc.api_get(session, budget_id, tenant_id=tenant_id, company_id=company_id)
    if b is None:
        raise HTTPException(404, "Budget not found")
    return BudgetOut.model_validate(b)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BudgetOut, status_code=201)
async def create_budget(
    request: Request,
    payload: BudgetCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    tenant_id = resolve_tenant_id(request)

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
        b = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            account_id=payload.account_id,
            year=payload.year,
            month=payload.month,
            amount=payload.amount,
            notes=payload.notes,
        )
    except (ValueError, svc.BudgetApiError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(b)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{budget_id}",
    responses={
        200: {"model": BudgetOut},
        409: {"model": BudgetConflictBody, "description": "Version mismatch"},
    },
)
async def update_budget(
    request: Request,
    budget_id: UUID,
    payload: BudgetUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with budget version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify budget belongs to this tenant
    if await svc.api_get(session, budget_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Budget not found")

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

    raw = payload.model_dump(exclude_unset=True)

    try:
        b = await svc.api_update(
            session,
            budget_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **raw,
        )
    except svc.VersionConflict as exc:
        body = BudgetConflictBody(
            detail="version mismatch",
            current=BudgetOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BudgetApiError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(b)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{budget_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": BudgetConflictBody, "description": "Version mismatch"},
    },
)
async def delete_budget(
    request: Request,
    budget_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, budget_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Budget not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "budgets", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with budget version is required")

    try:
        await svc.api_delete(
            session,
            budget_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        body = BudgetConflictBody(
            detail="version mismatch",
            current=BudgetOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BudgetApiError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)
