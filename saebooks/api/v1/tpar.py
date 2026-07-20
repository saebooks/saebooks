"""JSON router — /api/v1/tpar.

Australian Taxable Payments Annual Report (TPAR) endpoints.

Workflow:
  POST   /api/v1/tpar                  — generate a new DRAFT run for a FY
  GET    /api/v1/tpar                  — list runs for the active company
  GET    /api/v1/tpar/{id}             — run header (status, totals)
  GET    /api/v1/tpar/{id}/lines       — payee detail rows
  GET    /api/v1/tpar/{id}/lines.csv   — same rows as a downloadable CSV
  POST   /api/v1/tpar/{id}/finalise    — lock a DRAFT into FINALISED
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session

router = APIRouter(
    prefix="/tpar",
    tags=["tpar"],
    dependencies=[Depends(require_bearer)],
)


@router.get("")
async def list_tpar_runs(
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, fy_start, fy_end, status,
                       total_payee_count, total_gross_amount, total_gst_amount,
                       generated_at, finalised_at
                  FROM tpar_runs
                 WHERE company_id = :c AND tenant_id = :t
                   AND archived_at IS NULL
                 ORDER BY fy_start DESC, generated_at DESC
                """
            ),
            {"c": str(company_id), "t": str(tenant_id)},
        )
    ).all()
    return JSONResponse({
        "items": [
            {
                "id": str(r[0]),
                "fy_start": r[1].isoformat(), "fy_end": r[2].isoformat(),
                "status": r[3],
                "total_payee_count": r[4],
                "total_gross_amount": str(r[5]),
                "total_gst_amount": str(r[6]),
                "generated_at": r[7].isoformat() if r[7] else None,
                "finalised_at": r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    })


@router.post("", status_code=201)
async def create_tpar_run(
    request: Request,
    payload: dict = Body(...),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Generate (or regenerate) a TPAR run for the given FY.

    Body:
        {
          "fy_start": "2025-07-01",     // optional, defaults to current FY start
          "fy_end":   "2026-06-30",     // optional, fy_start + 1 year - 1 day
          "notes":    "…"
        }
    """
    from saebooks.jurisdictions.au.tpar import build_tpar_run

    tenant_id = resolve_tenant_id(request)
    fy_start_raw = payload.get("fy_start")
    fy_end_raw = payload.get("fy_end")
    notes = payload.get("notes")

    if fy_start_raw:
        try:
            fy_start = date.fromisoformat(str(fy_start_raw))
        except ValueError:
            raise HTTPException(422, "fy_start must be YYYY-MM-DD") from None
    else:
        today = date.today()
        # AU FY runs 1 July → 30 June.
        fy_start = date(today.year if today.month >= 7 else today.year - 1, 7, 1)

    if fy_end_raw:
        try:
            fy_end = date.fromisoformat(str(fy_end_raw))
        except ValueError:
            raise HTTPException(422, "fy_end must be YYYY-MM-DD") from None
    else:
        fy_end = date(fy_start.year + 1, 6, 30)

    if fy_end < fy_start:
        raise HTTPException(422, "fy_end must not be before fy_start")

    try:
        new_id = await build_tpar_run(
            session,
            tenant_id=tenant_id, company_id=company_id,
            fy_start=fy_start, fy_end=fy_end, notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return JSONResponse({
        "id": str(new_id),
        "fy_start": fy_start.isoformat(),
        "fy_end": fy_end.isoformat(),
        "status": "DRAFT",
    }, status_code=201)


@router.get("/{run_id}")
async def get_tpar_run(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.jurisdictions.au.tpar import get_tpar_run as _get
    tenant_id = resolve_tenant_id(request)
    row = await _get(session, tenant_id=tenant_id, company_id=company_id, run_id=run_id)
    if row is None:
        raise HTTPException(404, "TPAR run not found")
    return JSONResponse(row)


@router.get("/{run_id}/lines")
async def get_tpar_run_lines(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.jurisdictions.au.tpar import get_tpar_run as _get
    from saebooks.jurisdictions.au.tpar import list_tpar_lines
    tenant_id = resolve_tenant_id(request)
    if (await _get(session, tenant_id=tenant_id, company_id=company_id, run_id=run_id)) is None:
        raise HTTPException(404, "TPAR run not found")
    lines = await list_tpar_lines(session, tenant_id=tenant_id, run_id=run_id)
    return JSONResponse({"items": lines, "total": len(lines)})


@router.get("/{run_id}/lines.csv")
async def get_tpar_run_csv(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    from saebooks.jurisdictions.au.tpar import get_tpar_run as _get
    from saebooks.jurisdictions.au.tpar import lines_to_csv, list_tpar_lines
    tenant_id = resolve_tenant_id(request)
    if (await _get(session, tenant_id=tenant_id, company_id=company_id, run_id=run_id)) is None:
        raise HTTPException(404, "TPAR run not found")
    lines = await list_tpar_lines(session, tenant_id=tenant_id, run_id=run_id)
    csv_bytes = lines_to_csv(lines)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="tpar-{run_id}.csv"'},
    )


@router.post("/{run_id}/finalise")
async def finalise_tpar_run(
    run_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    from saebooks.jurisdictions.au.tpar import finalise_tpar_run as _final
    tenant_id = resolve_tenant_id(request)
    try:
        await _final(session, tenant_id=tenant_id, run_id=run_id,
                     finalised_by=f"api:{bearer[:8]}…")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse({"id": str(run_id), "status": "FINALISED"})
