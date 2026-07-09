"""JSON router — /api/v1/super_lodgements.

Payday Super Phase 1 lodgement endpoints. Tracks a super lodgement
per pay-run, emits the SAFF v1 CSV for manual portal upload, and lets
the operator mark a run submitted with the clearing-house receipt id.

Workflow:
  POST   /api/v1/super_lodgements                  — generate DRAFT from a pay_run_id
  GET    /api/v1/super_lodgements                  — list runs (filter by pay_run_id, status)
  GET    /api/v1/super_lodgements/{id}             — run header
  GET    /api/v1/super_lodgements/{id}/lines       — per-employee detail rows
  GET    /api/v1/super_lodgements/{id}/lines.csv   — SAFF v1 CSV download
  POST   /api/v1/super_lodgements/{id}/finalise    — DRAFT  → FINALISED
  POST   /api/v1/super_lodgements/{id}/mark-submitted
                                                    — FINALISED → SUBMITTED
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session

router = APIRouter(
    prefix="/super_lodgements",
    tags=["super_lodgements"],
    dependencies=[Depends(require_bearer)],
)


@router.get("")
async def list_super_lodgements(
    request: Request,
    pay_run_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.services.super_stream import list_super_lodgement_runs

    tenant_id = resolve_tenant_id(request)
    items = await list_super_lodgement_runs(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        pay_run_id=pay_run_id,
        status=status,
    )
    return JSONResponse({"items": items, "total": len(items)})


@router.post("", status_code=201)
async def create_super_lodgement(
    request: Request,
    payload: dict = Body(...),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Generate a super lodgement run for a pay run.

    Body::

        {"pay_run_id": "...", "notes": "optional"}
    """
    from saebooks.services.super_stream import (
        SuperLodgementError,
        build_super_lodgement_run,
        get_super_lodgement_run,
    )

    tenant_id = resolve_tenant_id(request)
    pay_run_raw = payload.get("pay_run_id")
    if not pay_run_raw:
        raise HTTPException(422, "pay_run_id is required")
    try:
        pay_run_id = UUID(str(pay_run_raw))
    except (ValueError, TypeError):
        raise HTTPException(422, "pay_run_id must be a valid UUID") from None

    notes = payload.get("notes")
    try:
        new_id = await build_super_lodgement_run(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            pay_run_id=pay_run_id,
            notes=notes,
        )
    except SuperLodgementError as exc:
        raise HTTPException(422, str(exc)) from exc

    run = await get_super_lodgement_run(
        session, tenant_id=tenant_id, company_id=company_id, run_id=new_id
    )
    return JSONResponse(run or {"id": str(new_id), "status": "DRAFT"}, status_code=201)


@router.get("/{run_id}")
async def get_super_lodgement(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.services.super_stream import get_super_lodgement_run

    tenant_id = resolve_tenant_id(request)
    row = await get_super_lodgement_run(
        session, tenant_id=tenant_id, company_id=company_id, run_id=run_id
    )
    if row is None:
        raise HTTPException(404, "Super lodgement run not found")
    return JSONResponse(row)


@router.get("/{run_id}/lines")
async def get_super_lodgement_lines(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.services.super_stream import (
        get_super_lodgement_run,
        list_super_lodgement_lines,
    )

    tenant_id = resolve_tenant_id(request)
    if (
        await get_super_lodgement_run(
            session, tenant_id=tenant_id, company_id=company_id, run_id=run_id
        )
    ) is None:
        raise HTTPException(404, "Super lodgement run not found")
    lines = await list_super_lodgement_lines(
        session, tenant_id=tenant_id, run_id=run_id
    )
    return JSONResponse({"items": lines, "total": len(lines)})


@router.get("/{run_id}/lines.csv")
async def get_super_lodgement_csv(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    from saebooks.services.super_stream import (
        get_super_lodgement_run,
        lines_to_saff_csv,
        list_super_lodgement_lines,
    )

    tenant_id = resolve_tenant_id(request)
    run = await get_super_lodgement_run(
        session, tenant_id=tenant_id, company_id=company_id, run_id=run_id
    )
    if run is None:
        raise HTTPException(404, "Super lodgement run not found")
    lines = await list_super_lodgement_lines(
        session, tenant_id=tenant_id, run_id=run_id
    )
    csv_bytes = lines_to_saff_csv(run, lines)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="saff-{run_id}.csv"',
        },
    )


@router.post("/{run_id}/finalise")
async def finalise_super_lodgement(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.services.super_stream import (
        SuperLodgementError,
        finalise_super_lodgement_run,
    )

    tenant_id = resolve_tenant_id(request)
    try:
        await finalise_super_lodgement_run(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            finalised_by=f"api:{bearer[:8]}…",
        )
    except SuperLodgementError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse({"id": str(run_id), "status": "FINALISED"})


@router.post("/{run_id}/mark-submitted")
async def mark_super_lodgement_submitted_route(
    run_id: UUID,
    request: Request,
    payload: dict = Body(...),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Operator confirms they've uploaded the SAFF + got a receipt.

    Body::

        {"reference": "clearing-house receipt id"}
    """
    from saebooks.services.super_stream import (
        SuperLodgementError,
        mark_super_lodgement_submitted,
    )

    tenant_id = resolve_tenant_id(request)
    reference = (payload.get("reference") or "").strip()
    if not reference:
        raise HTTPException(422, "reference is required")
    try:
        await mark_super_lodgement_submitted(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            reference=reference,
        )
    except SuperLodgementError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse({
        "id": str(run_id),
        "status": "SUBMITTED",
        "submitted_reference": reference,
    })
