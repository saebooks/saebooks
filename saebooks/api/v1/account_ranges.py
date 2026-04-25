"""JSON router — ``/api/v1/account_ranges``.

Phase 1 cycle 40.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* ``AccountRange`` has no ``version`` column — no optimistic locking.
* PATCH/DELETE operate by ID with company_id isolation.
* ``GET  /prefix_mode``  — returns the current prefix mode setting.
* ``PATCH /prefix_mode`` — updates the prefix mode (classic | extended).

NOTE: The ``/prefix_mode`` routes are registered before ``/{range_id}``
to avoid FastAPI routing the literal string "prefix_mode" as a UUID path param.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    AccountRangeCreate,
    AccountRangeListOut,
    AccountRangeOut,
    AccountRangeUpdate,
    PrefixModeOut,
    PrefixModeUpdate,
)
from saebooks.models.account_range import AccountRange
from saebooks.models.company import Company
from saebooks.services import accounts as account_svc
from saebooks.services import settings as settings_svc

router = APIRouter(
    prefix="/account_ranges",
    tags=["account_ranges"],
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


def _dump(rng: AccountRange) -> dict[str, Any]:
    return json.loads(AccountRangeOut.model_validate(rng).model_dump_json())


# ---------------------------------------------------------------------------
# Prefix mode — registered first so "prefix_mode" isn't treated as a UUID
# ---------------------------------------------------------------------------


@router.get("/prefix_mode", response_model=PrefixModeOut)
async def get_prefix_mode(
    session: AsyncSession = Depends(get_session),
) -> PrefixModeOut:
    """Return the current account-range prefix mode (classic | extended)."""
    mode = await account_svc.get_prefix_mode(session)
    return PrefixModeOut(mode=mode)


@router.patch("/prefix_mode", response_model=PrefixModeOut)
async def update_prefix_mode(
    payload: PrefixModeUpdate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> PrefixModeOut:
    """Update the prefix mode setting.

    Valid values: ``classic`` (single-digit prefixes 1-9) or
    ``extended`` (multi-digit prefixes of any width).
    """
    mode = payload.mode.strip().lower()
    if mode not in ("classic", "extended"):
        raise HTTPException(422, "mode must be 'classic' or 'extended'")

    await settings_svc.set(
        session,
        "prefix_mode",
        mode,
        updated_by=f"api:{bearer[:8]}…",
    )
    return PrefixModeOut(mode=mode)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=AccountRangeListOut)
async def list_account_ranges(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AccountRangeListOut:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    ranges = await account_svc.get_ranges(session, company_id)

    return AccountRangeListOut(
        items=[AccountRangeOut.model_validate(r) for r in ranges],
        total=len(ranges),
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=AccountRangeOut, status_code=201)
async def create_account_range(
    request: Request,
    payload: AccountRangeCreate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)
    try:
        rng = await account_svc.create_range(
            session,
            company_id,
            prefix=payload.prefix,
            label=payload.label,
            account_types=payload.account_types,
            sort_order=payload.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(422, "A range with that prefix already exists") from exc

    body = _dump(rng)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH — no If-Match; AccountRange has no version column)
# ---------------------------------------------------------------------------


@router.patch("/{range_id}", response_model=AccountRangeOut)
async def update_account_range(
    request: Request,
    range_id: UUID,
    payload: AccountRangeUpdate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)

    rng = await session.get(AccountRange, range_id)
    if rng is None or rng.company_id != company_id:
        raise HTTPException(404, "Account range not found")

    try:
        updated = await account_svc.update_range(
            session,
            range_id,
            label=payload.label,
            account_types=payload.account_types,
            sort_order=payload.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(updated)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (hard-delete — ranges have no archived_at)
# ---------------------------------------------------------------------------


@router.delete("/{range_id}", responses={204: {"description": "Deleted"}})
async def delete_account_range(
    request: Request,
    range_id: UUID,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    company_id = await _first_company_id(session, tenant_id)

    rng = await session.get(AccountRange, range_id)
    if rng is None or rng.company_id != company_id:
        raise HTTPException(404, "Account range not found")

    try:
        await account_svc.delete_range(session, range_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return Response(status_code=204)
