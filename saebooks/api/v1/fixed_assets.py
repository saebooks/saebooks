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

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    DepreciationRunAllRequest,
    DepreciationRunAllResponse,
    DepreciationRunAllResultItem,
    FixedAssetConflictBody,
    FixedAssetConvertToInventory,
    FixedAssetConvertToInventoryResponse,
    FixedAssetCreate,
    FixedAssetDepreciationRunRequest,
    FixedAssetDepreciationRunResponse,
    FixedAssetDispose,
    FixedAssetListOut,
    FixedAssetOut,
    FixedAssetUpdate,
)
from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import assets as legacy_assets_svc
from saebooks.services import fixed_assets as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/fixed_assets",
    tags=["fixed_assets"],
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


def _dump(asset: Any) -> dict[str, Any]:
    return json.loads(FixedAssetOut.model_validate(asset).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=FixedAssetListOut)
async def list_fixed_assets(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    depreciation_model_id: str | None = Query(default=None),
    archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> FixedAssetListOut:
    offset = (page - 1) * page_size
    tenant_id = resolve_tenant_id(request)
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
# Batch depreciation run — MUST be before /{asset_id} routes
# ---------------------------------------------------------------------------


@router.post(
    "/depreciation_run_all",
    response_model=DepreciationRunAllResponse,
)
async def depreciation_run_all(
    request: Request,
    body: DepreciationRunAllRequest,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Run depreciation for all active, non-disposed assets up to ``through``.

    Iterates every active asset for the company. Calls
    ``legacy_assets_svc.post_depreciation`` for each one and collects
    results. Per-asset errors are caught and added to the ``errors`` list
    rather than aborting the whole batch.

    Returns 200 with a :class:`DepreciationRunAllResponse` regardless of
    whether any assets had errors — the caller inspects the ``errors``
    field to handle partial failures.
    """
    actor = f"api:{bearer[:8]}…"
    resolve_tenant_id(request)

    # Fetch all active, non-archived assets for this company.
    result = await session.execute(
        select(FixedAsset).where(
            FixedAsset.company_id == company_id,
            FixedAsset.status == "active",
            FixedAsset.archived_at.is_(None),
        ).order_by(FixedAsset.code)
    )
    assets = list(result.scalars().all())

    results: list[DepreciationRunAllResultItem] = []
    errors: list[str] = []
    total_amount = Decimal("0")

    for asset in assets:
        try:
            _updated_asset, amount_posted = await legacy_assets_svc.post_depreciation(
                session,
                asset.id,
                body.through,
                posted_by=actor,
            )
            note = f"Posted AUD {amount_posted}" if amount_posted > 0 else "No depreciation to post"
            results.append(
                DepreciationRunAllResultItem(
                    asset_id=asset.id,
                    asset_code=asset.code,
                    amount_posted=amount_posted,
                    note=note,
                )
            )
            total_amount += amount_posted
        except Exception as exc:
            errors.append(f"{asset.code}: {exc}")

    response_body = DepreciationRunAllResponse(
        through=body.through,
        total_assets=len(results),
        total_amount=total_amount,
        results=results,
        errors=errors,
    )
    return JSONResponse(
        json.loads(response_body.model_dump_json()),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{asset_id}", response_model=FixedAssetOut)
async def get_fixed_asset(
    request: Request,
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> FixedAssetOut:
    tenant_id = resolve_tenant_id(request)
    asset = await svc.get(session, asset_id, tenant_id=tenant_id, company_id=company_id)
    if asset is None:
        raise HTTPException(404, "Fixed asset not found")
    return FixedAssetOut.model_validate(asset)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=FixedAssetOut, status_code=201)
async def create_fixed_asset(
    request: Request,
    payload: FixedAssetCreate,
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
        asset = await svc.create(
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
        await store_response(session, key, 201, json.dumps(body).encode())
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
    request: Request,
    asset_id: UUID,
    payload: FixedAssetUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")
    key = _parse_idempotency_key(idempotency_key)

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify asset belongs to this tenant
    if await svc.get(session, asset_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Fixed asset not found")

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

    try:
        asset = await svc.update(
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
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.FixedAssetApiError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(asset)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
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
    request: Request,
    asset_id: UUID,
    payload: FixedAssetDispose,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
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

    tenant_id = resolve_tenant_id(request)
    # Belt-and-braces: verify asset belongs to this tenant
    if await svc.get(session, asset_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Fixed asset not found")

    try:
        asset = await svc.dispose(
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
# Post depreciation (GL run → 200 with FixedAssetDepreciationRunResponse)
# ---------------------------------------------------------------------------


@router.post(
    "/{asset_id}/post_depreciation",
    responses={
        200: {"model": FixedAssetDepreciationRunResponse},
        409: {"model": FixedAssetConflictBody, "description": "Version mismatch"},
    },
)
async def post_depreciation(
    request: Request,
    asset_id: UUID,
    payload: FixedAssetDepreciationRunRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Run a depreciation posting for a fixed asset up to ``through``.

    Computes the incremental depreciation from
    ``last_depreciation_posted_through`` to ``through``, creates and
    posts a GL journal entry (Dr dep_expense, Cr accum_dep), advances
    the cursor, and bumps the asset version.

    Returns 422 if the asset is not ``active`` or if the service raises
    a ValueError (e.g. asset not found, bad model config).
    Returns 409 on version mismatch (with current state in body).
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")

    actor = f"api:{bearer[:8]}…"
    tenant_id = resolve_tenant_id(request)
    asset = await svc.get(session, asset_id, tenant_id=tenant_id, company_id=company_id)
    if asset is None:
        raise HTTPException(404, "Fixed asset not found")
    if asset.version != expected:
        body = FixedAssetConflictBody(
            detail="version mismatch",
            current=FixedAssetOut.model_validate(asset),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    if asset.status != "active":
        raise HTTPException(
            422,
            f"Cannot depreciate asset in status {asset.status!r} — must be active",
        )

    try:
        updated_asset, amount_posted = await legacy_assets_svc.post_depreciation(
            session,
            asset_id,
            payload.through,
            posted_by=actor,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # Bump version and append change_log after the GL posting.
    updated_asset.version = updated_asset.version + 1
    await session.flush()
    await session.refresh(updated_asset)

    from saebooks.services import change_log as change_log_svc  # local import avoids circular

    await change_log_svc.append(
        session,
        entity="fixed_asset",
        entity_id=updated_asset.id,
        op="depreciation_run",
        actor=actor,
        payload={"amount_posted": str(amount_posted), "through": str(payload.through)},
        version=updated_asset.version,
    )
    await session.commit()
    await session.refresh(updated_asset)

    note = (
        f"Depreciation posted: {amount_posted} through {payload.through}"
        if amount_posted > 0
        else f"No depreciation to post through {payload.through} (cursor already at or past date)"
    )
    response_body = FixedAssetDepreciationRunResponse(
        asset=FixedAssetOut.model_validate(updated_asset),
        amount_posted=amount_posted,
        note=note,
    )
    return JSONResponse(
        json.loads(response_body.model_dump_json()),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Convert to Inventory (demonstrator → used-vehicle stock)
# ---------------------------------------------------------------------------


@router.post(
    "/{asset_id}/convert_to_inventory",
    responses={
        201: {"model": FixedAssetConvertToInventoryResponse},
        409: {"model": FixedAssetConflictBody, "description": "Version mismatch"},
    },
    status_code=201,
)
async def convert_to_inventory(
    request: Request,
    asset_id: UUID,
    payload: FixedAssetConvertToInventory,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Convert an active FA demonstrator to used-inventory stock.

    Catches depreciation up to conversion_date, posts a balanced conversion
    journal (DR Inventory / DR Accum Dep / CR FA Cost), creates an inventory
    Item with on_hand_qty=1 at NBV, and marks the FA disposed at NBV proceeds.

    Requires If-Match: <version> for optimistic locking.
    Returns 201 with the disposed asset, new item id/sku, NBV, and journal id.
    Returns 409 on version conflict (current state in body).
    Returns 422 if asset is not ACTIVE or inputs are invalid.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")

    actor = f"api:{bearer[:8]}…"
    tenant_id = resolve_tenant_id(request)
    if await svc.get(session, asset_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Fixed asset not found")

    try:
        asset, item_id, item_sku, nbv, journal_id = await svc.convert_to_inventory(
            session,
            asset_id,
            actor=actor,
            expected_version=expected,
            conversion_date=payload.conversion_date,
            inventory_account_id=payload.inventory_account_id,
            cogs_account_id=payload.cogs_account_id,
            income_account_id=payload.income_account_id,
            sku=payload.sku,
            vin=payload.vin,
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

    response_body = FixedAssetConvertToInventoryResponse(
        asset=FixedAssetOut.model_validate(asset),
        item_id=item_id,
        item_sku=item_sku,
        nbv=nbv,
        journal_id=journal_id,
    )
    return JSONResponse(json.loads(response_body.model_dump_json()), status_code=201)


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
    request: Request,
    asset_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, asset_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Fixed asset not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "fixed_assets", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with asset version is required")

    try:
        await svc.delete(
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
