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
POST   /api/v1/tax_returns/generate            — compute + persist a real return (Packet 4c)
GET    /api/v1/tax_returns/{id}/export         — render the filable document (Packet 4c)
POST   /api/v1/tax_returns/{id}/file           — mark FILED, manual file-and-confirm (Packet 4c)
POST   /api/v1/tax_returns/{id}/lodge          — dispatch to the lodge-server
GET    /api/v1/tax_returns/{id}/lodgement      — fetch the lodgement_record (if any)

Packet 4c note — ``/generate`` and ``/export`` cover box-vector return
types (``tax_return_generator.generate_return``'s box-definition model —
AU's BAS/IAS, EE's KMD) PLUS the Estonian **TSD** list-shaped return,
which has its own dedicated generator (``services/lodgement/tsd``:
``generate_tsd`` + ``persist_tsd_return`` + ``build_tsd_xml_document``)
reading FINALIZED EE pay runs. TSD is dispatched to that path
(``_generate_tsd_return`` / ``_build_tsd_envelope``) rather than the box
model. The OTHER list-shaped types (KMD-INF's invoice/credit-note
listing, KMD-2027's XBRL detail rows) remain unwired — ``/generate``
raises 422 (from the same "no box definitions" ``ValueError``
``generate_return`` raises) and ``/export`` raises 501 for any
return_type without a document builder — loud, not silent.
"""
from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.services import business_identifiers
from saebooks.services.authz import require_permission_or_role_inline, require_user

router = APIRouter(
    prefix="/tax_returns",
    tags=["tax_returns"],
    dependencies=[Depends(require_bearer)],
)


async def _build_bas_envelope(
    session: AsyncSession,
    *,
    company_id: UUID,
    period_id: Any,
    figures: dict[str, Any],
    lodgement_fields: dict[str, Any] | None = None,
) -> bytes:
    """Build the AS.0004 Activity Statement document for a tax return.

    Resolves the reporting period (``tax_periods``) and the lodging entity's
    ABN (``companies``), maps the return's ``figures`` JSONB onto BAS labels,
    and renders the AS.0004 ``ASSubmitRequest`` XML. Raises 422 if the period
    is missing or a required reporting-party field is absent — we never emit
    a knowingly-invalid lodgement envelope.

    ``lodgement_fields`` carries the AS.0004 statement header the DB cannot
    derive, straight from the lodge request body: ``tfn`` (reporting party),
    ``document_id`` (the ATO DIN from the AS Get / prefill), ``signatory``,
    and optionally ``form_type`` (TypeC, default "A") + ``revision``.
    """
    from saebooks.services.lodgement.sbr import (
        AsDocumentError,
        BasFigures,
        ReportingContext,
        build_bas_document,
    )

    fields = lodgement_fields or {}

    prow = (
        await session.execute(
            text(
                "SELECT period_start, period_end FROM tax_periods "
                "WHERE id = :p AND company_id = :c"
            ),
            {"p": str(period_id), "c": str(company_id)},
        )
    ).first()
    if prow is None:
        raise HTTPException(422, "Tax period not found for this return")

    # The lodging entity's ABN, read from its ``au_abn`` business identifier
    # (the legacy ``companies.abn`` column was dropped in 0204).
    _abn_ident = await business_identifiers.get(session, company_id, "au_abn")
    abn = _abn_ident.value if _abn_ident is not None else None
    if not abn:
        raise HTTPException(
            422,
            "Company ABN is required to lodge a BAS/IAS — set it in company settings.",
        )

    figs = BasFigures.from_figures_json(figures)
    ctx = ReportingContext(
        abn=abn,
        period_start=prow[0],
        period_end=prow[1],
        tfn=str(fields.get("tfn") or ""),
        document_id=str(fields.get("document_id") or ""),
        form_type=str(fields.get("form_type") or "A"),
        revision=bool(fields.get("revision", False)),
        signatory=str(fields.get("signatory") or ""),
    )
    try:
        return build_bas_document(figs, ctx)
    except AsDocumentError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _generate_tsd_return(
    session: AsyncSession,
    *,
    company_id: UUID,
    tenant_id: uuid.UUID,
    period_id: UUID,
    period_start: Any,
    period_end: Any,
) -> JSONResponse:
    """Compute + persist an Estonian TSD return (Packet 4c, list-shaped path).

    TSD is NOT a box vector — it is a per-person payment listing (MAIN
    roll-up + Lisa-1 rows) assembled from FINALIZED EE pay runs by
    ``services.lodgement.tsd.generate_tsd``, so it deliberately bypasses
    ``generate_return``'s box-definition model (which raises "no box
    definitions" for it — the guarded set this branch moves TSD OUT of).

    Guards / semantics:

    * **Non-EE company -> 422.** Keyed on the company's OWN
      ``jurisdiction`` (authoritative), not the request payload's — a TSD
      is inherently Estonian and is sourced from the company's EE pay runs.
    * **Empty period -> valid nil declaration, not 422.** ``generate_tsd``
      returns an all-zero ``TsdListing`` when there are no posted EE pay
      runs; that is a real filable nil TSD (mirrors the generator's own
      contract), persisted like any other rather than rejected.
    * **Duplicate period -> no dedup.** Matches the box-vector path, which
      also just inserts a fresh row — there is no unique constraint on
      ``tax_returns`` and no replace-draft/409 precedent to mirror.
    """
    from saebooks.services.lodgement.tsd import generate_tsd, persist_tsd_return

    jrow = (
        await session.execute(
            text("SELECT jurisdiction FROM companies WHERE id = :c"),
            {"c": str(company_id)},
        )
    ).first()
    company_jurisdiction = jrow[0] if jrow is not None else None
    if company_jurisdiction != "EE":
        raise HTTPException(
            422,
            "TSD is an Estonian (EE) return type; the active company's "
            f"jurisdiction is {company_jurisdiction!r}. Only EE companies "
            "can generate a TSD.",
        )

    listing = await generate_tsd(
        session,
        company_id=company_id,
        period_start=period_start,
        period_end=period_end,
    )
    row = await persist_tsd_return(
        session, listing, tenant_id=tenant_id, period_id=period_id,
    )
    await session.commit()
    return JSONResponse(
        {
            "id": str(row.id),
            "jurisdiction": row.jurisdiction,
            "period_id": str(row.period_id),
            "return_type": row.return_type,
            "status": row.status.value,
            "figures": row.figures,
        },
        status_code=201,
    )


async def _build_tsd_envelope(
    session: AsyncSession,
    *,
    company_id: UUID,
    period_start: Any,
    period_end: Any,
) -> bytes:
    """Render a persisted TSD return's filable e-MTA ``tsd_vorm`` XML.

    Unlike the KMD export branch (which reconstructs ``KmdFigures`` from the
    persisted ``figures`` JSONB), TSD is **re-generated from its source
    FINALIZED pay runs** here, not rebuilt from ``figures``. Reason:
    ``persist_tsd_return`` MASKS the isikukood in the JSONB copy
    (``serializer._mask_isikukood`` — the plaintext national ID must not sit
    in a plain JSONB column that the read API echoes verbatim), but the
    filed XML needs the REAL isikukood as each Lisa-1 row key. The plaintext
    lives only in the live employee/pay-run data, so the document is built
    by re-running ``generate_tsd`` against the (locked, FINALIZED) pay runs
    — the one place the real isikukood is authoritatively available. The
    source rows are write-locked at FINALIZED, so this re-generation is
    stable for a given period.
    """
    from saebooks.services.lodgement.tsd import (
        TsdReportingContext,
        build_tsd_xml_document,
        generate_tsd,
    )

    # Estonian äriregistri kood, read from its own ``ee_regcode`` business
    # identifier — same explicit lookup the KMD export branch uses.
    _ident = await business_identifiers.get(session, company_id, "ee_regcode")
    regcode = (_ident.value if _ident is not None else "") or ""
    listing = await generate_tsd(
        session,
        company_id=company_id,
        period_start=period_start,
        period_end=period_end,
    )
    ctx = TsdReportingContext(
        regcode=regcode, period_start=period_start, period_end=period_end,
    )
    return build_tsd_xml_document(listing, ctx)


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
        # 0199 (Packet 4c) — trailing column, appended not inserted, so
        # every existing positional index above is untouched.
        "filed_at": row[11].isoformat() if len(row) > 11 and row[11] else None,
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
               status, lodgement_record_id, filed_at
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
                       status, lodgement_record_id, filed_at
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


@router.post("/generate", status_code=201)
async def generate_tax_return(
    request: Request,
    payload: dict = Body(...),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Compute a real return from the ledger and persist it (status READY).

    Unlike ``POST /tax_returns`` (a bare DRAFT shell for hand-built
    figures), this actually aggregates the company's ledger against the
    jurisdiction/return_type's box definitions via
    ``tax_return_generator.generate_return`` + ``persist_return`` — the
    same path ``reports.bas_summary`` uses for AU BAS, generalised to
    any box-vector return type (e.g. EE's KMD).

    Body:
        {
            "jurisdiction": "EE",
            "period_id":    "<UUID of tax_periods row>",
            "return_type":  "KMD" | "BAS" | "IAS" | ...
        }

    Box-vector return types (with ``tax_return_box_definitions`` rows)
    plus the Estonian TSD (``return_type == "TSD"``, dispatched to
    ``_generate_tsd_return`` — its own pay-run-listing generator) are
    supported. The remaining list-shaped types (KMD-INF, KMD-2027) raise
    422 here; use their own dedicated generator functions directly.
    """
    from saebooks.services import tax_return_generator as generator_svc

    tenant_id = resolve_tenant_id(request)
    try:
        jurisdiction = str(payload["jurisdiction"])[:3]
        period_id = UUID(str(payload["period_id"]))
        return_type = str(payload["return_type"])[:32]
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"missing or invalid field: {exc}") from exc

    prow = (
        await session.execute(
            text(
                "SELECT period_start, period_end FROM tax_periods "
                "WHERE id = :p AND company_id = :c"
            ),
            {"p": str(period_id), "c": str(company_id)},
        )
    ).first()
    if prow is None:
        raise HTTPException(422, "Tax period not found for this return")

    # TSD is list-shaped (per-person pay-run rows), not a box vector — it
    # has moved OUT of the guarded set into its own dedicated generator
    # path (``_generate_tsd_return``), rather than the box-definition model
    # below that raises the "no box definitions" 422 for it.
    if return_type == "TSD":
        return await _generate_tsd_return(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            period_id=period_id,
            period_start=prow[0],
            period_end=prow[1],
        )

    try:
        result = await generator_svc.generate_return(
            session,
            company_id,
            jurisdiction=jurisdiction,
            return_type=return_type,
            from_date=prow[0],
            to_date=prow[1],
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        # No box definitions for this jurisdiction/return_type — a real
        # config gap (e.g. a list-shaped type like TSD), not a bug.
        # Loud 422, never a silently-empty return.
        raise HTTPException(422, str(exc)) from exc

    row = await generator_svc.persist_return(
        session,
        result,
        company_id=company_id,
        tenant_id=tenant_id,
        period_id=period_id,
    )
    await session.commit()
    return JSONResponse(
        {
            "id": str(row.id),
            "jurisdiction": row.jurisdiction,
            "period_id": str(row.period_id),
            "return_type": row.return_type,
            "status": row.status.value,
            "figures": row.figures,
        },
        status_code=201,
    )


@router.get("/{return_id}/export")
async def export_tax_return(
    return_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Render the persisted return's filable document (XML/CSV bytes).

    Dispatches by (jurisdiction, return_type) to the matching document
    builder:

    * AU BAS/IAS   -> ``services.lodgement.sbr.build_bas_document``
      (same builder ``/lodge`` uses, reused here for a plain download —
      no lodge-server round trip).
    * EE KMD       -> ``services.lodgement.kmd.serializer.build_kmd_xml_document``,
      reconstructing ``KmdFigures`` from the persisted ``figures`` JSONB
      (the exact round-trip ``persist_return``'s docstring documents).
    * EE TSD       -> ``_build_tsd_envelope`` (``services.lodgement.tsd.
      build_tsd_xml_document``), RE-GENERATED from the source FINALIZED
      pay runs rather than the persisted ``figures`` — the JSONB copy masks
      the isikukood, which the filed XML needs in full (see that helper).

    Any other (jurisdiction, return_type) — including the remaining
    list-shaped types (KMD-INF, KMD-2027) that have no
    ``figures``->typed-object reconstruction today — raises 501, not a
    best-effort guess.
    """
    tenant_id = resolve_tenant_id(request)
    row = (
        await session.execute(
            text(
                """
                SELECT id, jurisdiction, period_id, return_type, figures
                  FROM tax_returns
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                """
            ),
            {"id": str(return_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Tax return not found")

    jurisdiction, period_id, return_type, figures = row[1], row[2], row[3], row[4]
    figures = figures if isinstance(figures, dict) else {}

    if jurisdiction == "AU" and return_type in ("BAS", "IAS"):
        body = await _build_bas_envelope(
            session, company_id=company_id, period_id=period_id, figures=figures
        )
        media_type = "application/xml"
    elif jurisdiction == "EE" and return_type == "KMD":
        from saebooks.services.lodgement.kmd.serializer import (
            KmdFigures,
            KmdReportingContext,
            build_kmd_xml_document,
        )

        prow = (
            await session.execute(
                text(
                    "SELECT period_start, period_end FROM tax_periods "
                    "WHERE id = :p AND company_id = :c"
                ),
                {"p": str(period_id), "c": str(company_id)},
            )
        ).first()
        if prow is None:
            raise HTTPException(422, "Tax period not found for this return")
        # The Estonian registry code (äriregistri kood) for a KMD filer, read
        # explicitly from its own ``ee_regcode`` business identifier. The
        # legacy ``companies.abn`` column that overloaded this (an ABN for AU
        # companies, the regcode for EE) was dropped in 0198; each code now
        # lives under its correctly-typed scheme. Same explicit lookup that
        # services.lodgement.kmd_2027.generator.generate_kmd_2027 uses.
        _ident = await business_identifiers.get(session, company_id, "ee_regcode")
        regcode = (_ident.value if _ident is not None else "") or ""
        kmd_figures = KmdFigures.from_figures_json(figures)
        ctx = KmdReportingContext(
            regcode=regcode, period_start=prow[0], period_end=prow[1]
        )
        body = build_kmd_xml_document(kmd_figures, ctx)
        media_type = "application/xml"
    elif jurisdiction == "EE" and return_type == "TSD":
        prow = (
            await session.execute(
                text(
                    "SELECT period_start, period_end FROM tax_periods "
                    "WHERE id = :p AND company_id = :c"
                ),
                {"p": str(period_id), "c": str(company_id)},
            )
        ).first()
        if prow is None:
            raise HTTPException(422, "Tax period not found for this return")
        body = await _build_tsd_envelope(
            session, company_id=company_id,
            period_start=prow[0], period_end=prow[1],
        )
        media_type = "application/xml"
    else:
        raise HTTPException(
            501,
            f"Export not implemented for jurisdiction={jurisdiction!r} "
            f"return_type={return_type!r}.",
        )

    filename = f"{return_type}_{return_id}.xml"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{return_id}/file")
async def file_tax_return(
    return_id: UUID,
    request: Request,
    payload: dict = Body(default={}),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Mark a persisted return FILED — the manual file-and-confirm path.

    Distinct from ``/lodge`` (which dispatches to the automated SBR/
    X-Road rail and sets status ``lodged``): this is for a return the
    accountant filed themselves outside any automated rail (e.g. via
    EMTA's e-service portal for a KMD/TSD the engine doesn't yet lodge
    automatically). Stamps ``filed_at`` and sets ``status='filed'``.

    Body (all optional): ``{"reference": "<filer's own confirmation
    reference/receipt number>"}`` — stored as-is in ``figures.filed_reference``
    if supplied; no schema on it, this is a free-text confirmation note.
    """
    tenant_id = resolve_tenant_id(request)
    row = (
        await session.execute(
            text(
                """
                SELECT id, status, figures FROM tax_returns
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                """
            ),
            {"id": str(return_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Tax return not found")
    if row[1] == "filed":
        raise HTTPException(422, f"Tax return {return_id} is already filed")

    reference = payload.get("reference")
    figures = row[2] if isinstance(row[2], dict) else {}
    if reference:
        figures = {**figures, "filed_reference": str(reference)}

    result = (
        await session.execute(
            text(
                """
                UPDATE tax_returns
                   SET status = 'filed', filed_at = now(), figures = CAST(:f AS jsonb)
                 WHERE id = :id
             RETURNING filed_at
                """
            ),
            {"id": str(return_id), "f": __import__("json").dumps(figures)},
        )
    ).first()
    await session.commit()
    return JSONResponse({
        "return_id": str(return_id),
        "status": "filed",
        "filed_at": result[0].isoformat() if result and result[0] else None,
    })


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
        LodgementAuthError,
        LodgementEditionError,
        LodgementService,
        LodgementUnsupportedEdition,
        LodgementUpstreamUnavailable,
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

    # Permission gate, inline (not a static Depends()) because the
    # correct catalogue code depends on return_type, only known after
    # this row lookup. Conservative default (tax_return.lodge) for any
    # type not explicitly mapped below.
    _LODGE_CODE_BY_TYPE = {
        "BAS": "bas.lodge",
        "IAS": "bas.lodge",
        "STP_PAYEVENT": "payroll.run",
        "TPAR": "tpar.finalise",
        "SUPERSTREAM": "super_lodgement.finalise",
    }
    await require_permission_or_role_inline(
        _LODGE_CODE_BY_TYPE.get(return_type, "tax_return.lodge"),
        require_user(),
        request,
    )

    figures = row[5] if isinstance(row[5], dict) else {}
    envelope_b64 = payload.get("envelope_b64")
    if envelope_b64:
        # Caller supplied a pre-built envelope (e.g. an external generator).
        envelope = base64.b64decode(envelope_b64)
    elif return_type in ("BAS", "IAS"):
        # Generate the AS.0004 Activity Statement XML from the return's figures.
        envelope = await _build_bas_envelope(
            session,
            company_id=company_id,
            period_id=row[3],
            figures=figures,
            lodgement_fields=payload,
        )
    elif return_type == "STP_PAYEVENT" and figures.get("payees") is not None:
        # STP normally lodges via the StpSubmission flow, not tax_returns; this
        # branch only fires if a caller persisted a build_pay_event payload as
        # the return's figures.
        from saebooks.services.lodgement.sbr import (
            StpDocumentError,
            build_stp_pay_event_document,
        )

        try:
            envelope = build_stp_pay_event_document(figures).to_envelope_bundle()
        except StpDocumentError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        # No generator for this return_type yet (TPAR / SuperStream / foreign).
        envelope = (
            b"<!-- placeholder envelope; SBR generator not available for "
            b"this return_type -->"
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
