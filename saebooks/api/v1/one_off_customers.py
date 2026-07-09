"""Pure JSON router for /api/v1/one-off-customers.

Mirrors the contacts router pattern: Bearer-token auth, If-Match optimistic
locking on PATCH/DELETE, defence-in-depth tenant scoping on every operation.
Tests deferred — will be added in a follow-up commit.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    OneOffCustomerConflictBody,
    OneOffCustomerCreate,
    OneOffCustomerListOut,
    OneOffCustomerOut,
    OneOffCustomerUpdate,
)
from saebooks.services import one_off_customers as svc

router = APIRouter(
    prefix="/one-off-customers",
    tags=["one-off-customers"],
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


def _dump(customer: Any) -> dict[str, Any]:
    return json.loads(OneOffCustomerOut.model_validate(customer).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=OneOffCustomerListOut)
async def list_one_off_customers(
    search: str | None = Query(default=None, alias="q"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> OneOffCustomerListOut:
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_active(
        session,
        company_id,
        tenant_id=tenant_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return OneOffCustomerListOut(
        items=[OneOffCustomerOut.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{customer_id}", response_model=OneOffCustomerOut)
async def get_one_off_customer(
    customer_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> OneOffCustomerOut:
    tenant_id = resolve_tenant_id(request)
    customer = await svc.get(session, customer_id, tenant_id=tenant_id, company_id=company_id)
    if customer is None:
        raise HTTPException(404, "One-off customer not found")
    return OneOffCustomerOut.model_validate(customer)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=OneOffCustomerOut, status_code=201)
async def create_one_off_customer(
    payload: OneOffCustomerCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    try:
        customer = await svc.create(
            session,
            company_id,
            tenant_id=tenant_id,
            actor=f"api:{bearer[:8]}…",
            **payload.model_dump(exclude_unset=False),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await session.refresh(customer)
    return JSONResponse(_dump(customer), status_code=201)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{customer_id}",
    responses={
        200: {"model": OneOffCustomerOut},
        409: {"model": OneOffCustomerConflictBody, "description": "Version mismatch"},
    },
)
async def update_one_off_customer(
    customer_id: UUID,
    payload: OneOffCustomerUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with customer version is required")
    tenant_id = resolve_tenant_id(request)

    if await svc.get(session, customer_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "One-off customer not found")

    try:
        customer = await svc.update(
            session,
            customer_id,
            tenant_id=tenant_id,
            company_id=company_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = OneOffCustomerConflictBody(
            detail="version mismatch",
            current=OneOffCustomerOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(customer)
    return JSONResponse(_dump(customer), status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive)
# ---------------------------------------------------------------------------


@router.delete(
    "/{customer_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": OneOffCustomerConflictBody, "description": "Version mismatch"},
    },
)
async def archive_one_off_customer(
    customer_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with customer version is required")
    tenant_id = resolve_tenant_id(request)

    if await svc.get(session, customer_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "One-off customer not found")

    try:
        customer = await svc.archive(
            session,
            customer_id,
            tenant_id=tenant_id,
            company_id=company_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = OneOffCustomerConflictBody(
            detail="version mismatch",
            current=OneOffCustomerOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    if customer is None:
        raise HTTPException(404, "One-off customer not found")
    return Response(status_code=204)
