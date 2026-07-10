"""Module routes for the imports wizard — thin shell over the engine's
``api/v1/imports`` handlers' service calls.

Every endpoint runs the SAME engine logic in-process (Wizard state store,
idempotency claim, per-kind commit dispatch, change-log append) against the
shared DB with RLS bound to the ``X-Tenant-Id`` header. The engine's
``imports.py`` commit helpers (``_commit_bank`` / ``_commit_coa`` /
``_commit_bill_csv`` / ``_commit_qbo``) and Wizard/idempotency helpers are
imported and reused verbatim — this router only re-implements the thin HTTP
wrapper (token gate + header-derived tenant/company + response shaping) that
the engine router otherwise derives from the JWT.

Parity notes vs the engine router
---------------------------------
* Tenant comes from ``X-Tenant-Id`` (not the JWT); the active company for the
  commit step comes from ``X-Company-Id`` (the delegating engine resolves it
  from ``get_active_company_id`` and forwards it).
* The QBO Pro+ **edition gate** (``_check_qbo_flag``) is NOT re-applied here:
  the module has no JWT to derive the per-user edition from. In delegated
  mode the engine has already gated the ``qbo`` kind at ``start`` time before
  forwarding. This is a documented delegated-mode nuance — the capture split
  is flag-off by default, so it has zero impact until capture is deployed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from capture_app.deps import (
    TenantContext,
    get_module_session,
    get_tenant_context,
    require_capture_token,
)
from saebooks.api.v1._wizard import (
    Wizard,
    WizardExpiredError,
    WizardNotFoundError,
)
from saebooks.api.v1.imports import (
    _QBO_KINDS,
    WizardStartBody,
    WizardStepBody,
    _commit_bank,
    _commit_bill_csv,
    _commit_coa,
    _commit_qbo,
    _parse_idempotency_key,
    _wizard_summary,
)
from saebooks.services import change_log as change_log_svc
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/imports",
    tags=["capture-imports"],
    dependencies=[Depends(require_capture_token)],
)


@router.post("/wizards", status_code=201)
async def start_wizard(
    payload: WizardStartBody,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_module_session),
) -> Any:
    """Start a new import wizard session (mirrors ``POST /api/v1/imports/wizards``)."""
    tenant_id = ctx.tenant_id
    key = _parse_idempotency_key(idempotency_key)

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
                {"code": "request_in_flight", "message": "Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    initial_state = dict(payload.initial)
    initial_state.setdefault("step", 0)
    initial_state.setdefault("kind", payload.kind)

    wizard_id = await Wizard.start(
        session,
        kind=payload.kind,
        initial_state=initial_state,
        ttl_seconds=payload.ttl_seconds,
    )

    body = _wizard_summary(wizard_id, initial_state)
    await session.commit()

    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()

    return JSONResponse(body, status_code=201)


@router.post("/wizards/{wizard_id}/step", status_code=200)
async def advance_wizard_step(
    wizard_id: UUID,
    payload: WizardStepBody,
    session: AsyncSession = Depends(get_module_session),
    _tok: None = Depends(require_capture_token),
) -> Any:
    """Apply a partial state patch and advance the step counter."""
    patch = dict(payload.patch)
    patch["step"] = payload.step + 1

    try:
        merged = await Wizard.step(session, wizard_id, patch)
    except WizardNotFoundError:
        raise HTTPException(404, "Wizard not found or expired") from None
    except WizardExpiredError:
        raise HTTPException(410, "Wizard has expired — start a new one") from None

    await session.commit()

    return JSONResponse({
        "step": merged.get("step", payload.step + 1),
        "state": merged,
        "completed": bool(merged.get("_completed", False)),
    })


@router.get("/wizards", status_code=200)
async def list_wizards(
    kind: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_module_session),
    _tok: None = Depends(require_capture_token),
) -> Any:
    """List the tenant's non-expired import wizards and their state."""
    if limit < 1 or limit > 500:
        raise HTTPException(422, "limit must be between 1 and 500")
    wizards = await Wizard.list_active(session, kind=kind, limit=limit)
    return JSONResponse({"wizards": wizards, "total": len(wizards)})


@router.get("/wizards/{wizard_id}", status_code=200)
async def get_wizard(
    wizard_id: UUID,
    session: AsyncSession = Depends(get_module_session),
    _tok: None = Depends(require_capture_token),
) -> Any:
    """Return the current wizard state (without mutating it)."""
    state = await Wizard.get(session, wizard_id)
    if state is None:
        raise HTTPException(404, "Wizard not found or expired")
    return JSONResponse({
        "wizard_id": str(wizard_id),
        "step": state.get("step", 0),
        "state": state,
    })


@router.post("/wizards/{wizard_id}/commit", status_code=200)
async def commit_wizard(
    wizard_id: UUID,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_module_session),
) -> Any:
    """Run the import and persist the results (mirrors the engine commit)."""
    tenant_id = ctx.tenant_id
    if ctx.company_id is None:
        raise HTTPException(400, "X-Company-Id header is required to commit an import")
    company_id = ctx.company_id
    key = _parse_idempotency_key(idempotency_key)

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
                {"code": "request_in_flight", "message": "Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    state = await Wizard.get(session, wizard_id)
    if state is None:
        raise HTTPException(404, "Wizard not found or expired")

    kind = state.get("kind", "")

    result: dict[str, Any]
    if kind in ("bank_csv", "bank_ofx"):
        result = await _commit_bank(session, state, company_id)
    elif kind == "coa":
        result = await _commit_coa(session, state, company_id)
    elif kind == "bill_csv":
        result = await _commit_bill_csv(session, state, company_id, tenant_id)
    elif kind in _QBO_KINDS:
        result = await _commit_qbo(session, state, company_id)
    else:
        raise HTTPException(422, f"Unknown import kind: {kind!r}")

    await change_log_svc.append(
        session,
        entity="import_wizard",
        entity_id=wizard_id,
        op="create",
        actor="capture-module",
        payload={"kind": kind, "result": result},
        version=1,
        tenant_id=tenant_id,
    )

    await session.commit()

    body = result
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()

    return JSONResponse(body)
