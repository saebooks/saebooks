"""JSON router — ``/api/v1/credit_notes``.

Phase 1 tier-3 credit notes endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Lines are nested in the response.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_active_user_id, get_session
from saebooks.api.v1.edit_force_gate import edit_force_admin_gate
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    CreditNoteConflictBody,
    CreditNoteCreate,
    CreditNoteListOut,
    CreditNoteOut,
    CreditNoteUpdate,
)
from saebooks.models.credit_note import CreditNoteStatus
from saebooks.services import credit_notes as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/credit_notes",
    tags=["credit_notes"],
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


def _dump(cn: Any) -> dict[str, Any]:
    return json.loads(CreditNoteOut.model_validate(cn).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=CreditNoteListOut)
async def list_credit_notes(
    request: Request,
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> CreditNoteListOut:
    offset = (page - 1) * page_size
    status_enum: CreditNoteStatus | None = None
    if status is not None:
        try:
            status_enum = CreditNoteStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    notes, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        contact_id=contact_id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return CreditNoteListOut(
        items=[CreditNoteOut.model_validate(cn) for cn in notes],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{credit_note_id}", response_model=CreditNoteOut)
async def get_credit_note(
    request: Request,
    credit_note_id: UUID,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> CreditNoteOut:
    tenant_id = resolve_tenant_id(request)
    cn = await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id)
    if cn is None:
        raise HTTPException(404, "Credit note not found")
    return CreditNoteOut.model_validate(cn)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=CreditNoteOut, status_code=201)
async def create_credit_note(
    request: Request,
    payload: CreditNoteCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
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
        cn = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            lines=[line.model_dump() for line in payload.lines],
            original_invoice_id=payload.original_invoice_id,
            reason=payload.reason,
            notes=payload.notes,
            payment_terms=payload.payment_terms,
        )
    except (ValueError, svc.CreditNoteError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(cn)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{credit_note_id}",
    responses={
        200: {"model": CreditNoteOut},
        409: {"model": CreditNoteConflictBody, "description": "Version mismatch"},
    },
)
async def update_credit_note(
    request: Request,
    credit_note_id: UUID,
    payload: CreditNoteUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with credit note version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Credit note not found")

    try:
        lines_data = (
            [line.model_dump() for line in payload.lines]
            if payload.lines is not None
            else None
        )
        cn = await svc.api_update(
            session,
            credit_note_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            lines=lines_data,
            original_invoice_id=payload.original_invoice_id,
            reason=payload.reason,
            notes=payload.notes,
            payment_terms=payload.payment_terms,
        )
    except svc.VersionConflict as exc:
        body = CreditNoteConflictBody(
            detail="version mismatch",
            current=CreditNoteOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.CreditNoteError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(cn)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{credit_note_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": CreditNoteConflictBody, "description": "Version mismatch"},
    },
)
async def void_credit_note(
    request: Request,
    credit_note_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Credit note not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "credit_notes", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with credit note version is required")

    try:
        if existing.status == svc.CreditNoteStatus.DRAFT:
            # DELETE soft-deletes: a DRAFT archives (no JE reversal, op="archive").
            # A POSTED credit note voids with a reversing JE. The POST /{id}/void
            # action stays strict (api_void_credit_note rejects DRAFT with 422).
            await svc.api_void(
                session,
                credit_note_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        else:
            await svc.api_void_credit_note(
                session,
                credit_note_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                tenant_id=tenant_id,
            )
    except svc.VersionConflict as exc:
        body = CreditNoteConflictBody(
            detail="version mismatch",
            current=CreditNoteOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.CreditNoteError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{credit_note_id}/post",
    responses={
        200: {"model": CreditNoteOut},
        409: {"model": CreditNoteConflictBody, "description": "Version mismatch"},
    },
)
async def post_credit_note(
    request: Request,
    credit_note_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition credit note DRAFT → POSTED, generating journal entry lines."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with credit note version is required")

    tenant_id = resolve_tenant_id(request)
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
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    if await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Credit note not found")

    try:
        cn = await svc.api_post_credit_note(
            session,
            credit_note_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        body = CreditNoteConflictBody(
            detail="version mismatch",
            current=CreditNoteOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.CreditNoteError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(cn)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void via status transition (POST /{id}/void → VOIDED with JE reversal)
# ---------------------------------------------------------------------------


@router.post(
    "/{credit_note_id}/void",
    responses={
        204: {"description": "Voided"},
        409: {"model": CreditNoteConflictBody, "description": "Version mismatch"},
    },
)
async def void_credit_note_transition(
    request: Request,
    credit_note_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition POSTED credit note → VOIDED, reversing the journal entry."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with credit note version is required")

    tenant_id = resolve_tenant_id(request)
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
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 204,
            )

    if await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Credit note not found")

    try:
        await svc.api_void_credit_note(
            session,
            credit_note_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        body = CreditNoteConflictBody(
            detail="version mismatch",
            current=CreditNoteOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.CreditNoteError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    if key is not None:
        await store_response(session, key, 204, b'{"voided": true}')
        await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# PDF — LaTeX engine
# ---------------------------------------------------------------------------


def _build_credit_note_ctx(
    cn: Any, customer: Any, company: Any, bank_account: Any = None
) -> dict[str, Any]:
    """Build the document.tex.j2 render context from a CreditNote + Contact + Company.

    ``bank_account`` is the company's account flagged ``show_on_invoice``;
    its details render in the credit note's Remit-to block, falling back to
    the company's static ``bank_*`` columns (0168).
    """
    from saebooks.services.bank_accounts import remit_bank_details

    customer_addr: dict[str, Any] = {}
    if customer:
        customer_addr = {k: v for k, v in {
            "address_line1": customer.address_line1,
            "city":          customer.city,
            "state":         customer.state,
            "postcode":      customer.postcode,
            "country":       customer.country,
        }.items() if v}
    company_addr = (company.address or {}) if company else {}
    return {
        "kind":         "Credit Note",
        "number":       cn.number or str(cn.id)[:8],
        "issue_date":   cn.issue_date.isoformat() if cn.issue_date else "",
        "due_date":     "",          # credit notes have no due_date field
        "currency":     "AUD",
        "subtotal":     str(cn.subtotal),
        "tax_total":    str(cn.tax_total),
        "total":        str(cn.total),
        "amount_paid":  str(cn.amount_allocated),   # allocated = credited/paid amount
        "notes":        cn.notes or "",
        "payment_terms": cn.payment_terms or "",
        # Remit-to bank details (0171): flagged account first, company
        # bank_* columns as fallback.
        "bank_details": remit_bank_details(company, bank_account),
        "company": {
            "name":    (company.legal_name or company.name) if company else "",
            "abn":     (company.abn or "") if company else "",
            "phone":   (company.phone or "") if company else "",
            "email":   (company.email or "") if company else "",
            "website": (company.website or "") if company else "",
            "address": company_addr,
            **({k: v for k, v in company_addr.items()} if company_addr else {}),
        },
        "contact": {
            "name":    customer.name if customer else "",
            "email":   (customer.email or "") if customer else "",
            "phone":   (customer.phone or "") if customer else "",
            **({k: v for k, v in customer_addr.items()} if customer_addr else {}),
        },
        "lines": [
            {
                "line_no":     ln.line_no,
                "description": ln.description,
                "quantity":    str(ln.quantity),
                "unit_price":  str(ln.unit_price),
                "line_total":  str(ln.line_total),
                "line_tax":    str(ln.line_tax),
            }
            for ln in cn.lines
        ],
    }


@router.get("/{credit_note_id}/render-context")
async def get_credit_note_render_context(
    credit_note_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Return the fact context the app render service needs to build the CN PDF.

    Exact ``_build_credit_note_ctx`` dict (including the show_on_invoice bank
    resolution) fed to the render service by the /pdf route. The builder already
    sets ``kind`` = "Credit Note" inside ctx; it is also returned at top level.
    """
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.bank_accounts import get_remittance_account

    tenant_id = resolve_tenant_id(request)
    cn = await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id)
    if cn is None:
        raise HTTPException(404, "Credit note not found")

    customer = (
        await session.execute(sa_select(Contact).where(Contact.id == cn.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == cn.company_id))
    ).scalars().first()
    bank_account = await get_remittance_account(session, cn.company_id)

    ctx = _build_credit_note_ctx(cn, customer, company, bank_account)
    return JSONResponse({"template": "document", "kind": "Credit Note", "ctx": ctx})


@router.get("/{credit_note_id}/pdf")
async def get_credit_note_pdf(
    credit_note_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Render a credit note as PDF via the LaTeX engine. Always regenerated; never stored."""
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.latex_pdf import LatexCompileError, LatexServiceError, render_latex

    tenant_id = resolve_tenant_id(request)
    cn = await svc.api_get(session, credit_note_id, tenant_id=tenant_id, company_id=company_id)
    if cn is None:
        raise HTTPException(404, "Credit note not found")

    customer = (
        await session.execute(sa_select(Contact).where(Contact.id == cn.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == cn.company_id))
    ).scalars().first()
    from saebooks.services.bank_accounts import get_remittance_account
    bank_account = await get_remittance_account(session, cn.company_id)

    ctx = _build_credit_note_ctx(cn, customer, company, bank_account)
    try:
        pdf_bytes = await render_latex("document", ctx)
    except LatexCompileError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LaTeX compile error: {exc.log_tail}",
        ) from exc
    except LatexServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LaTeX service error: {exc}",
        ) from exc

    filename = f"credit-note-{ctx['number']}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
