"""JSON router — ``/api/v1/fixed_assets``.

Phase 1 tier-4 fixed-assets endpoint.

Fixed assets are capitalised items with a depreciation schedule.
This endpoint covers CRUD + listing only — depreciation journal posting,
disposal flow, and the monthly batch are separate cycles.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` on POST.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-archive (archived_at set) returning 204.
* Disposal restriction: PATCH on a ``disposed`` asset only allows
  cosmetic changes (description, extra) — other fields → 422.
* Archive restriction: ``DELETE`` on an active asset with remaining
  book value returns 422 "Cannot archive active asset with remaining
  book value — dispose first".
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.schemas import (
    FixedAssetConflictBody,
    FixedAssetCreate,
    FixedAssetDispose,
    FixedAssetListOut,
    FixedAssetOut,
    FixedAssetUpdate,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.idempotency_key import IdempotencyKey
from saebooks.services import fixed_assets as svc

router = APIRouter(
    prefix="/fixed_assets",
    tags=["fixed_assets"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session) -> UUID:
    """Return the first active company — Phase 1 single-company assumption."""
    result = await session.execute(
        select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(500, "No active company")
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


def _dump(asset: Any) -> dict[str, Any]:
    return json.loads(FixedAssetOut.model_validate(asset).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=FixedAssetListOut)
async def list_fixed_assets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    depreciation_model_id: str | None = Query(default=None),
    archived: bool = Query(default=False),
) -> FixedAssetListOut:
    offset = (page - 1) * page_size
    async with AsyncSessionLocal() as session:
        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        items, total = await svc.list_fixed_assets(
            session,
            company_id,
            tenant_id,
            status=status,
            depreciation_model_id=depreciation_model_id,
            archived=archived,
            limit=page_size,
            offset=offset,
        )
        return FixedAssetListOut(
            items=[FixedAssetOut.model_validate(a) for a in items],
            total=total,
            limit=page_size,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{asset_id}", response_model=FixedAssetOut)
async def get_fixed_asset(asset_id: UUID) -> FixedAssetOut:
    async with AsyncSessionLocal() as session:
        asset = await svc.api_get(session, asset_id)
        if asset is None:
            raise HTTPException(404, "Fixed asset not found")
        return FixedAssetOut.model_validate(asset)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=FixedAssetOut, status_code=201)
async def create_fixed_asset(
    payload: FixedAssetCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    key = _parse_idempotency_key(idempotency_key)
    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        company_id = await _first_company_id(session)
        tenant_id = resolve_tenant_id()
        try:
            asset = await svc.api_create(
                session,
                company_id,
                tenant_id,
                actor=f"api:{bearer[:8]}…",
                name=payload.name,
                depreciation_model_id=payload.depreciation_model_id,
                cost_account_id=payload.cost_account_id,
                accum_dep_account_id=payload.accum_dep_account_id,
                dep_expense_account_id=payload.dep_expense_account_id,
                purchase_date=payload.purchase_date,
                cost=payload.cost,
                in_service_date=payload.in_service_date,
                residual_value=payload.residual_value,
                code=payload.code,
                description=payload.description,
                tax_model_id=payload.tax_model_id,
                serial_number=payload.serial_number,
                manufacturer=payload.manufacturer,
                model_number=payload.model_number,
                location=payload.location,
                custody_person=payload.custody_person,
                warranty_end=payload.warranty_end,
                extra=payload.extra,
            )
        except (ValueError, svc.FixedAssetApiError) as exc:
            raise HTTPException(422, str(exc)) from exc

        body = _dump(asset)
        if key is not None:
            await _remember_idempotent(session, key, body, 201)
            await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{asset_id}",
    responses={
        200: {"model": FixedAssetOut},
        409: {"model": FixedAssetConflictBody, "description": "Version mismatch"},
    },
)
async def update_fixed_asset(
    asset_id: UUID,
    payload: FixedAssetUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")
    key = _parse_idempotency_key(idempotency_key)

    async with AsyncSessionLocal() as session:
        if key is not None:
            replay = await _idempotent_replay(session, key)
            if replay is not None:
                return replay

        try:
            asset = await svc.api_update(
                session,
                asset_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                **payload.model_dump(exclude_unset=True),
            )
        except svc.VersionConflict as exc:
            body = FixedAssetConflictBody(
                detail="version mismatch",
                current=FixedAssetOut.model_validate(exc.current),
            ).model_dump(mode="json")
            if key is not None:
                await _remember_idempotent(session, key, body, 409)
                await session.commit()
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.FixedAssetApiError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

        body = _dump(asset)
        if key is not None:
            await _remember_idempotent(session, key, body, 200)
            await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Dispose (state transition → 200 with updated FixedAssetOut)
# ---------------------------------------------------------------------------


@router.post(
    "/{asset_id}/dispose",
    responses={
        200: {"model": FixedAssetOut},
        409: {"model": FixedAssetConflictBody, "description": "Version mismatch"},
    },
)
async def dispose_fixed_asset(
    asset_id: UUID,
    payload: FixedAssetDispose,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    """Dispose a fixed asset.

    Marks the asset ``disposed``, stamps disposal_date and proceeds,
    and bumps the version. Requires ``If-Match: <version>`` for
    optimistic locking.

    Returns 422 if the asset is already disposed.
    Returns 409 on version conflict (with current state in body).
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")

    async with AsyncSessionLocal() as session:
        try:
            asset = await svc.api_dispose(
                session,
                asset_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                disposal_date=payload.disposal_date,
                proceeds=payload.proceeds,
                notes=payload.notes,
            )
        except svc.VersionConflict as exc:
            body = FixedAssetConflictBody(
                detail="version mismatch",
                current=FixedAssetOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.FixedAssetApiError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

    return JSONResponse(_dump(asset), status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-archive → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{asset_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": FixedAssetConflictBody, "description": "Version mismatch"},
    },
)
async def delete_fixed_asset(
    asset_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")

    async with AsyncSessionLocal() as session:
        try:
            await svc.api_delete(
                session,
                asset_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        except svc.VersionConflict as exc:
            body = FixedAssetConflictBody(
                detail="version mismatch",
                current=FixedAssetOut.model_validate(exc.current),
            ).model_dump(mode="json")
            return JSONResponse(body, status_code=409)
        except (ValueError, svc.FixedAssetApiError) as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(404, msg) from exc
            raise HTTPException(422, msg) from exc

    return Response(status_code=204)
