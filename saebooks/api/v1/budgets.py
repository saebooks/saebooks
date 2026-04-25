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

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    BudgetConflictBody,
    BudgetCreate,
    BudgetListOut,
    BudgetOut,
    BudgetUpdate,
)
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import budgets as svc

router = APIRouter(
    prefix="/budgets",
    tags=["budgets"],
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
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _parse_idempotency_key(header: str | None) -> UUID | None:
    if header is None or not header.strip():
        return None
    try:
        return UUID(header.strip())
    except ValueError as exc:
        raise HTTPException(400, "X-Idempotency-Key must be a UUID") from exc


async def _idempotent_replay(session, key: UUID) -> JSONResponse | None:
    existing = await session.get(IdempotencyKey, key)
    if existing is None:
        return None
    return JSONResponse(content=existing.response_body, status_code=existing.response_status)


async def _remember_idempotent(
    session, key: UUID, body: dict[str, Any], status_code: int
) -> None:
    row = IdempotencyKey(key=key, response_body=body, response_status=status_code)
    session.add(row)
    await session.flush()


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
) -> BudgetListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
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
) -> BudgetOut:
    tenant_id = resolve_tenant_id(request)
    b = await svc.api_get(session, budget_id, tenant_id=tenant_id)
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
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    if key is not None:
        replay = await _idempotent_replay(session, key)
        if replay is not None:
            return replay

    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
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
        await _remember_idempotent(session, key, body, 201)
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
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with budget version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify budget belongs to this tenant
    if await svc.api_get(session, budget_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Budget not found")

    if key is not None:
        replay = await _idempotent_replay(session, key)
        if replay is not None:
            return replay

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
            await _remember_idempotent(session, key, body, 409)
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BudgetApiError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(b)
    if key is not None:
        await _remember_idempotent(session, key, body, 200)
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
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with budget version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, budget_id, tenant_id=tenant_id) is None:
        raise HTTPException(404, "Budget not found")

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
