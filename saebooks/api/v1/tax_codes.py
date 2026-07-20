"""Pure JSON tax codes router — ``/api/v1/tax_codes``.

Phase 1 tier-1 entity. Follows the accounts pattern:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log`` (handled by the service layer).
* Jinja ``/tax-codes`` routes remain untouched — same service layer.
* TaxCode is CompanyScoped — list/create resolve company via the
  shared ``get_active_company_id`` dep (honours ``X-Company-Id``;
  falls back to first active company for tenant).

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; every query is gated by the ``tenant_isolation`` RLS
policy from migration 0055. ``svc.get`` is called with ``tenant_id``
so a foreign-tenant UUID returns ``None`` (404) even if the row
exists.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    TaxCodeConflictBody,
    TaxCodeCreate,
    TaxCodeListOut,
    TaxCodeOut,
    TaxCodeUpdate,
)
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import tax_codes as svc
from saebooks.services.authz import no_additional_gate, require_permission_or_role
from saebooks.services.hard_delete import hard_delete_with_audit

router = APIRouter(
    prefix="/tax_codes",
    tags=["tax_codes"],
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


def _dump(tax_code: TaxCode) -> dict[str, Any]:
    return json.loads(TaxCodeOut.model_validate(tax_code).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=TaxCodeListOut)
async def list_tax_codes(
    tax_system: str | None = Query(default=None),
    jurisdiction: str | None = Query(
        default=None,
        description=(
            "Filter by jurisdiction. Defaults to the requesting company's "
            "own jurisdiction (Company.jurisdiction) so the international "
            "reference codes stay hidden; pass an empty string to return "
            "all jurisdictions, or an explicit code to override."
        ),
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> TaxCodeListOut:
    # No query param at all -> default to the active company's own
    # jurisdiction (same pattern as Company.jurisdiction resolution in
    # pay_runs_v2 / journal.py). An explicit empty string means "all
    # jurisdictions"; any other explicit value overrides the default.
    if jurisdiction is None:
        juris = (
            await session.execute(
                select(Company.jurisdiction).where(Company.id == company_id)
            )
        ).scalar_one_or_none() or "AU"
    else:
        juris = jurisdiction or None
    count_stmt = (
        select(func.count())
        .select_from(TaxCode)
        .where(TaxCode.company_id == company_id, TaxCode.archived_at.is_(None))
    )
    if tax_system is not None:
        count_stmt = count_stmt.where(TaxCode.tax_system == tax_system)
    if juris is not None:
        count_stmt = count_stmt.where(TaxCode.jurisdiction == juris)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(TaxCode)
        .where(TaxCode.company_id == company_id, TaxCode.archived_at.is_(None))
        .order_by(TaxCode.code)
        .offset(offset)
        .limit(limit)
    )
    if tax_system is not None:
        stmt = stmt.where(TaxCode.tax_system == tax_system)
    if juris is not None:
        stmt = stmt.where(TaxCode.jurisdiction == juris)
    items = list((await session.execute(stmt)).scalars().all())
    return TaxCodeListOut(
        items=[TaxCodeOut.model_validate(tc) for tc in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{tax_code_id}", response_model=TaxCodeOut)
async def get_tax_code(
    tax_code_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> TaxCodeOut:
    tenant_id = resolve_tenant_id(request)
    tc = await svc.get(session, tax_code_id, tenant_id=tenant_id, company_id=company_id)
    if tc is None:
        raise HTTPException(404, "Tax code not found")
    return TaxCodeOut.model_validate(tc)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=TaxCodeOut,
    status_code=201,
    dependencies=[
        Depends(require_permission_or_role("tax_code.manage", no_additional_gate))
    ],
)
async def create_tax_code(
    payload: TaxCodeCreate,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    try:
        tc = await svc.create_for_api(
            session,
            company_id,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
            code=payload.code,
            name=payload.name,
            rate=payload.rate,
            tax_system=payload.tax_system,
            reporting_type=payload.reporting_type,
            description=payload.description,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    await session.refresh(tc)
    body = _dump(tc)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{tax_code_id}",
    responses={
        200: {"model": TaxCodeOut},
        409: {"model": TaxCodeConflictBody, "description": "Version mismatch"},
    },
    dependencies=[
        Depends(require_permission_or_role("tax_code.manage", no_additional_gate))
    ],
)
async def update_tax_code(
    tax_code_id: UUID,
    payload: TaxCodeUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with tax code version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, tax_code_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Tax code not found")

    try:
        tc = await svc.update_with_version(
            session,
            tax_code_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = TaxCodeConflictBody(
            detail="version mismatch",
            current=TaxCodeOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(tc)
    body = _dump(tc)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive via archived_at)
# ---------------------------------------------------------------------------


@router.delete(
    "/{tax_code_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": TaxCodeConflictBody, "description": "Version mismatch"},
    },
    dependencies=[
        Depends(require_permission_or_role("tax_code.manage", no_additional_gate))
    ],
)
async def archive_tax_code(
    tax_code_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, tax_code_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Tax code not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "tax_codes", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with tax code version is required")

    try:
        tc = await svc.archive_with_version(
            session,
            tax_code_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = TaxCodeConflictBody(
            detail="version mismatch",
            current=TaxCodeOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    if tc is None:
        raise HTTPException(404, "Tax code not found")
    return Response(status_code=204)
