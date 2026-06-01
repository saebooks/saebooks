"""JSON router — /api/v1/tax_returns.

CRUD over the existing tax_returns + lodgement_records tables, plus a
/lodge endpoint that dispatches to the appropriate LodgementService
method (lodge_stp / lodge_bas / lodge_tpar / send_superstream).

Scope: scaffolding. The endpoints round-trip tax_returns records and
record lodgement_records via the LodgementService factory. Building
the SBR3/STP envelopes from `figures` JSONB is a separate task (the
envelope generators live in saebooks/services/tax_engine/, this router
will adopt them as they land).

Routes
------
GET    /api/v1/tax_returns                     — list (filter by period, type, status)
GET    /api/v1/tax_returns/{id}                — detail (with lodgement_record if any)
POST   /api/v1/tax_returns                     — create a DRAFT
POST   /api/v1/tax_returns/{id}/lodge          — dispatch to the lodge-server
GET    /api/v1/tax_returns/{id}/lodgement      — fetch the lodgement_record (if any)
"""
from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session

router = APIRouter(
    prefix="/tax_returns",
    tags=["tax_returns"],
    dependencies=[Depends(require_bearer)],
)


def _serialise_return(row: Any) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "company_id": str(row[1]),
        "tenant_id": str(row[2]),
        "jurisdiction": row[3],
        "period_id": str(row[4]),
        "return_type": row[5],
        "figures": row[6],
        "generated_at": row[7].isoformat() if row[7] else None,
        "generated_by_user_id": str(row[8]) if row[8] else None,
        "status": row[9],
        "lodgement_record_id": str(row[10]) if row[10] else None,
    }


@router.get("")
async def list_tax_returns(
    request: Request,
    period_id: UUID | None = Query(default=None),
    return_type: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)
    where = ["company_id = :c", "tenant_id = :t"]
    params: dict[str, Any] = {"c": str(company_id), "t": str(tenant_id)}
    if period_id is not None:
        where.append("period_id = :p")
        params["p"] = str(period_id)
    if return_type is not None:
        where.append("return_type = :rt")
        params["rt"] = return_type
    if status_filter is not None:
        where.append("status = :s")
        params["s"] = status_filter
    sql = text(
        f"""
        SELECT id, company_id, tenant_id, jurisdiction, period_id,
               return_type, figures, generated_at, generated_by_user_id,
               status, lodgement_record_id
          FROM tax_returns
         WHERE {" AND ".join(where)}
         ORDER BY generated_at DESC
        """
    )
    rows = (await session.execute(sql, params)).all()
    return JSONResponse({"items": [_serialise_return(r) for r in rows], "total": len(rows)})


@router.get("/{return_id}")
async def get_tax_return(
    return_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)
    row = (
        await session.execute(
            text(
                """
                SELECT id, company_id, tenant_id, jurisdiction, period_id,
                       return_type, figures, generated_at, generated_by_user_id,
                       status, lodgement_record_id
                  FROM tax_returns
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                """
            ),
            {"id": str(return_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Tax return not found")
    return JSONResponse(_serialise_return(row))


@router.post("", status_code=201)
async def create_tax_return(
    request: Request,
    payload: dict = Body(...),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Create a new DRAFT tax return.

    Body:
        {
            "jurisdiction": "AU",
            "period_id":    "<UUID of tax_periods row>",
            "return_type":  "BAS" | "IAS" | "TPAR" | "STP_PAYEVENT",
            "figures":      { "G1": "..." }
        }
    """
    tenant_id = resolve_tenant_id(request)
    try:
        jurisdiction = str(payload["jurisdiction"])[:3]
        period_id = UUID(str(payload["period_id"]))
        return_type = str(payload["return_type"])[:32]
        figures = payload.get("figures") or {}
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"missing or invalid field: {exc}") from exc

    new_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO tax_returns
              (id, company_id, tenant_id, jurisdiction, period_id,
               return_type, figures, status)
            VALUES (:id, :c, :t, :j, :p, :rt, CAST(:f AS jsonb), 'draft')
            """
        ),
        {
            "id": str(new_id), "c": str(company_id), "t": str(tenant_id),
            "j": jurisdiction, "p": str(period_id), "rt": return_type,
            "f": __import__("json").dumps(figures),
        },
    )
    await session.commit()
    return JSONResponse({
        "id": str(new_id),
        "jurisdiction": jurisdiction,
        "period_id": str(period_id),
        "return_type": return_type,
        "status": "draft",
    }, status_code=201)


@router.post("/{return_id}/lodge")
async def lodge_tax_return(
    return_id: UUID,
    request: Request,
    payload: dict = Body(default={}),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Dispatch the return to the lodge-server.

    Routes by ``return_type``:
      * STP_PAYEVENT  -> LodgementService.lodge_stp
      * BAS / IAS     -> LodgementService.lodge_bas
      * TPAR          -> LodgementService.lodge_tpar
      * SUPERSTREAM   -> LodgementService.send_superstream

    The actual envelope (SBR3 XML) is expected on the request body as
    base64 ``envelope_b64``. If omitted, a placeholder envelope is
    sent — useful for smoke-testing the round-trip without the real
    XML generator. Real envelope construction lives in
    ``saebooks/services/tax_engine/`` and will be wired in as those
    modules land.
    """
    import base64
    from saebooks.api.v1.deps import get_lodgement
    from saebooks.services.lodgement import (
        LodgementAuthError, LodgementEditionError, LodgementService,
        LodgementUnsupportedEdition, LodgementUpstreamUnavailable,
    )

    tenant_id = resolve_tenant_id(request)
    row = (
        await session.execute(
            text(
                """
                SELECT id, return_type, jurisdiction, period_id, status, figures
                  FROM tax_returns
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                """
            ),
            {"id": str(return_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Tax return not found")
    if row[4] not in ("draft", "ready"):
        raise HTTPException(422, f"Cannot lodge return in status '{row[4]}'")

    return_type = row[1]
    envelope_b64 = payload.get("envelope_b64")
    if envelope_b64:
        envelope = base64.b64decode(envelope_b64)
    else:
        envelope = (
            b"<!-- placeholder envelope; real SBR3 XML from tax_engine pending -->"
        )

    metadata = {
        "return_id": str(return_id),
        "jurisdiction": row[2],
        "period_id": str(row[3]),
    }

    # Resolve service via dep
    svc: LodgementService = await get_lodgement.__wrapped__(request) if hasattr(get_lodgement, "__wrapped__") else get_lodgement(request)  # type: ignore[arg-type]
    try:
        if return_type == "STP_PAYEVENT":
            result = await svc.lodge_stp(envelope, str(return_id), metadata)
        elif return_type in ("BAS", "IAS"):
            result = await svc.lodge_bas(envelope, str(row[3]), metadata)
        elif return_type == "TPAR":
            fy_id = f"FY{row[3]}"
            result = await svc.lodge_tpar(envelope, fy_id, metadata)
        elif return_type == "SUPERSTREAM":
            result = await svc.send_superstream(envelope, str(return_id), metadata)
        else:
            raise HTTPException(422, f"Unknown return_type '{return_type}'")
    except LodgementUnsupportedEdition as exc:
        raise HTTPException(402, str(exc)) from exc
    except LodgementAuthError as exc:
        raise HTTPException(401, exc.detail) from exc
    except LodgementEditionError as exc:
        raise HTTPException(403, exc.detail) from exc
    except LodgementUpstreamUnavailable as exc:
        raise HTTPException(502, exc.detail) from exc

    # Persist the lodgement_record
    lr_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO lodgement_records (
                id, company_id, tenant_id, tax_return_id, jurisdiction,
                regulator, regulator_reference, status, submitted_at,
                request_blob, response_blob
            ) VALUES (
                :id, :c, :t, :rid, :j,
                :reg, :ref, :status, now(),
                CAST(:req AS jsonb), CAST(:res AS jsonb)
            )
            """
        ),
        {
            "id": str(lr_id), "c": str(company_id), "t": str(tenant_id),
            "rid": str(return_id), "j": row[2],
            "reg": "ATO", "ref": getattr(result, "receipt_id", None),
            "status": "accepted" if getattr(result, "ok", False) else "failed",
            "req": __import__("json").dumps({"envelope_b64": envelope_b64}),
            "res": __import__("json").dumps({
                "ok": getattr(result, "ok", None),
                "receipt_id": getattr(result, "receipt_id", None),
                "raw": getattr(result, "raw", None),
            }, default=str),
        },
    )
    await session.execute(
        text(
            """
            UPDATE tax_returns SET status = 'lodged',
                                   lodgement_record_id = :lr
             WHERE id = :id
            """
        ),
        {"lr": str(lr_id), "id": str(return_id)},
    )
    await session.commit()

    return JSONResponse({
        "return_id": str(return_id),
        "lodgement_record_id": str(lr_id),
        "ok": getattr(result, "ok", None),
        "receipt_id": getattr(result, "receipt_id", None),
    })


@router.get("/{return_id}/lodgement")
async def get_tax_return_lodgement(
    return_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)
    row = (
        await session.execute(
            text(
                """
                SELECT lr.id, lr.regulator, lr.regulator_reference,
                       lr.status, lr.submitted_at, lr.request_blob,
                       lr.response_blob
                  FROM lodgement_records lr
                  JOIN tax_returns tr ON tr.lodgement_record_id = lr.id
                 WHERE tr.id = :id AND tr.company_id = :c AND tr.tenant_id = :t
                """
            ),
            {"id": str(return_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "No lodgement record for this return")
    return JSONResponse({
        "id": str(row[0]),
        "regulator": row[1],
        "regulator_reference": row[2],
        "status": row[3],
        "submitted_at": row[4].isoformat() if row[4] else None,
        "request_blob": row[5],
        "response_blob": row[6],
    })
