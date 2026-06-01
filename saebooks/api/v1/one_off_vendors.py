"""Pure JSON router for /api/v1/one-off-vendors.

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
    OneOffVendorCreate,
    OneOffVendorListOut,
    OneOffVendorOut,
    OneOffVendorUpdate,
    OneOffVendorConflictBody,
)
from saebooks.services import one_off_vendors as svc

router = APIRouter(
    prefix="/one-off-vendors",
    tags=["one-off-vendors"],
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


def _dump(vendor: Any) -> dict[str, Any]:
    return json.loads(OneOffVendorOut.model_validate(vendor).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=OneOffVendorListOut)
async def list_one_off_vendors(
    search: str | None = Query(default=None, alias="q"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> OneOffVendorListOut:
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_active(
        session,
        company_id,
        tenant_id=tenant_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return OneOffVendorListOut(
        items=[OneOffVendorOut.model_validate(v) for v in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{vendor_id}", response_model=OneOffVendorOut)
async def get_one_off_vendor(
    vendor_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> OneOffVendorOut:
    tenant_id = resolve_tenant_id(request)
    vendor = await svc.get(session, vendor_id, tenant_id=tenant_id, company_id=company_id)
    if vendor is None:
        raise HTTPException(404, "One-off vendor not found")
    return OneOffVendorOut.model_validate(vendor)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=OneOffVendorOut, status_code=201)
async def create_one_off_vendor(
    payload: OneOffVendorCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    try:
        vendor = await svc.create(
            session,
            company_id,
            tenant_id=tenant_id,
            actor=f"api:{bearer[:8]}…",
            **payload.model_dump(exclude_unset=False),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await session.refresh(vendor)
    return JSONResponse(_dump(vendor), status_code=201)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{vendor_id}",
    responses={
        200: {"model": OneOffVendorOut},
        409: {"model": OneOffVendorConflictBody, "description": "Version mismatch"},
    },
)
async def update_one_off_vendor(
    vendor_id: UUID,
    payload: OneOffVendorUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with vendor version is required")
    tenant_id = resolve_tenant_id(request)

    if await svc.get(session, vendor_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "One-off vendor not found")

    try:
        vendor = await svc.update(
            session,
            vendor_id,
            tenant_id=tenant_id,
            company_id=company_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = OneOffVendorConflictBody(
            detail="version mismatch",
            current=OneOffVendorOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(vendor)
    return JSONResponse(_dump(vendor), status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive)
# ---------------------------------------------------------------------------


@router.delete(
    "/{vendor_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": OneOffVendorConflictBody, "description": "Version mismatch"},
    },
)
async def archive_one_off_vendor(
    vendor_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with vendor version is required")
    tenant_id = resolve_tenant_id(request)

    if await svc.get(session, vendor_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "One-off vendor not found")

    try:
        vendor = await svc.archive(
            session,
            vendor_id,
            tenant_id=tenant_id,
            company_id=company_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = OneOffVendorConflictBody(
            detail="version mismatch",
            current=OneOffVendorOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    if vendor is None:
        raise HTTPException(404, "One-off vendor not found")
    return Response(status_code=204)
