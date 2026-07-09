"""JSON router — ``/api/v1/bills``.

Phase 1 tier-3 accounts-payable endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Lines are nested in the response.

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; all queries within the request are gated by the
``tenant_isolation`` RLS policy from migration 0055. The active
company is resolved by the shared ``get_active_company_id`` dep —
callers may pin a specific company via ``X-Company-Id``; otherwise
the first active company for the tenant is used. Existence checks
pass ``tenant_id`` to ``svc.api_get`` so a foreign-tenant UUID
returns 404 even if the caller knows the id.
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
    BillConflictBody,
    BillCreate,
    BillListOut,
    BillOut,
    BillUpdate,
)
from saebooks.models.bill import BillStatus
from saebooks.services import bills as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/bills",
    tags=["bills"],
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


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(bill: Any) -> dict[str, Any]:
    return json.loads(BillOut.model_validate(bill).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=BillListOut)
async def list_bills(
    request: Request,
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BillListOut:
    offset = (page - 1) * page_size
    status_enum: BillStatus | None = None
    if status is not None:
        try:
            status_enum = BillStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    bills, total = await svc.list_active(
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
    return BillListOut(
        items=[BillOut.model_validate(b) for b in bills],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{bill_id}", response_model=BillOut)
async def get_bill(
    bill_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> BillOut:
    tenant_id = resolve_tenant_id(request)
    bill = await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id)
    if bill is None:
        raise HTTPException(404, "Bill not found")
    return BillOut.model_validate(bill)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=BillOut, status_code=201)
async def create_bill(
    payload: BillCreate,
    request: Request,
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
        bill = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            due_date=payload.due_date,
            lines=[line.model_dump() for line in payload.lines],
            reference=payload.supplier_reference,
            notes=payload.notes,
            currency=payload.currency,
            fx_rate=payload.fx_rate,
        )
    except (ValueError, svc.BillError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(bill)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{bill_id}",
    responses={
        200: {"model": BillOut},
        409: {"model": BillConflictBody, "description": "Version mismatch"},
    },
)
async def update_bill(
    bill_id: UUID,
    payload: BillUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bill version is required")

    tenant_id = resolve_tenant_id(request)
    if await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Bill not found")

    try:
        lines_data = (
            [line.model_dump() for line in payload.lines]
            if payload.lines is not None
            else None
        )
        bill = await svc.api_update(
            session,
            bill_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            due_date=payload.due_date,
            notes=payload.notes,
            reference=payload.supplier_reference,
            currency=payload.currency,
            fx_rate=payload.fx_rate,
            lines=lines_data,
        )
    except svc.VersionConflict as exc:
        body = BillConflictBody(
            detail="version mismatch",
            current=BillOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BillError) as exc:
        msg = str(exc)
        # CIVL-1: cross-tenant FK injection is a validation error (422),
        # not a "missing entity" 404 — the supplied UUID exists, it just
        # doesn't belong to the caller's tenant.
        if "not found in current tenant" in msg.lower():
            raise HTTPException(422, msg) from exc
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(bill)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{bill_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": BillConflictBody, "description": "Version mismatch"},
    },
)
async def void_bill(
    bill_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Bill not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "bills", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bill version is required")

    try:
        if existing.status == svc.BillStatus.DRAFT:
            # DELETE soft-deletes: a DRAFT archives (no JE reversal, op="archive").
            # POST /{id}/void stays strict (api_void_bill rejects DRAFT 422).
            await svc.api_void(
                session,
                bill_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        else:
            await svc.api_void_bill(
                session,
                bill_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                tenant_id=tenant_id,
                actor_user_id=await get_active_user_id(request),
            )
    except svc.VersionConflict as exc:
        body = BillConflictBody(
            detail="version mismatch",
            current=BillOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BillError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{bill_id}/post",
    responses={
        200: {"model": BillOut},
        409: {"model": BillConflictBody, "description": "Version mismatch"},
    },
)
async def post_bill(
    bill_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition bill DRAFT → POSTED, generating journal entry lines."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bill version is required")

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

    if await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Bill not found")

    try:
        bill = await svc.api_post_bill(
            session,
            bill_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        body = BillConflictBody(
            detail="version mismatch",
            current=BillOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BillError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(bill)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void via status transition (POST /{id}/void → VOIDED with JE reversal)
# ---------------------------------------------------------------------------


@router.post(
    "/{bill_id}/void",
    responses={
        200: {"model": BillOut},
        409: {"model": BillConflictBody, "description": "Version mismatch"},
    },
)
async def void_bill_transition(
    bill_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition any non-VOIDED bill → VOIDED, reversing JE if POSTED."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with bill version is required")

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

    if await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Bill not found")

    try:
        bill = await svc.api_void_bill(
            session,
            bill_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        body = BillConflictBody(
            detail="version mismatch",
            current=BillOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.BillError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(bill)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# PDF + send-email — added 2026-05-26. Parity with quotes/invoices. Bills
# are usually supplier-received (not sent), but bookkeepers sometimes need
# to forward a bill copy to staff/auditors — same pipeline, same kill switch.
# ---------------------------------------------------------------------------


def _build_bill_ctx(bill: Any, supplier: Any, company: Any) -> dict[str, Any]:
    """Construct render-document ctx from a bill + supplier + company."""
    supplier_addr = {}
    if supplier:
        supplier_addr = {k: v for k, v in {
            "address_line1": supplier.address_line1,
            "address_line2": supplier.address_line2,
            "city":          supplier.city,
            "state":         supplier.state,
            "postcode":      supplier.postcode,
            "country":       supplier.country,
        }.items() if v}
    company_addr = (company.address or {}) if company else {}
    return {
        "number":      bill.number or str(bill.id)[:8],
        "issue_date":  bill.issue_date.isoformat() if bill.issue_date else "",
        "due_date":    bill.due_date.isoformat() if bill.due_date else "",
        "currency":    bill.currency,
        "subtotal":    str(bill.subtotal),
        "tax_total":   str(bill.tax_total),
        "total":       str(bill.total),
        "amount_paid": str(bill.amount_paid),
        "notes":       bill.notes or "",
        # No bank_details / Remit-to on bills: a bill is money the company
        # OWES — rendering our own bank details would invite mispayment.
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
            "name":    supplier.name if supplier else "",
            "email":   (supplier.email or "") if supplier else "",
            "phone":   (supplier.phone or "") if supplier else "",
            **({k: v for k, v in supplier_addr.items()} if supplier_addr else {}),
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
            for ln in bill.lines
        ],
    }


@router.get("/{bill_id}/render-context")
async def get_bill_render_context(
    bill_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Return the fact context the app render service needs to build the bill PDF.

    Exact ``_build_bill_ctx`` dict fed to the render service by the /pdf route.
    Bills carry NO bank_details (a bill is money the company owes). ``kind`` is
    returned alongside ``ctx``.
    """
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact

    tenant_id = resolve_tenant_id(request)
    bill = await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id)
    if bill is None:
        raise HTTPException(404, "Bill not found")

    supplier = (
        await session.execute(sa_select(Contact).where(Contact.id == bill.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == bill.company_id))
    ).scalars().first()

    ctx = _build_bill_ctx(bill, supplier, company)
    return JSONResponse({"template": "document", "kind": "Bill", "ctx": ctx})


@router.get("/{bill_id}/pdf")
async def get_bill_pdf(
    bill_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Render a bill as PDF (Bill layout). Always regenerated; never stored."""
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.latex_pdf import render_latex

    tenant_id = resolve_tenant_id(request)
    bill = await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id)
    if bill is None:
        raise HTTPException(404, "Bill not found")

    supplier = (
        await session.execute(sa_select(Contact).where(Contact.id == bill.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == bill.company_id))
    ).scalars().first()

    ctx = _build_bill_ctx(bill, supplier, company)
    ctx.setdefault("kind", "Bill")
    pdf_bytes = await render_latex("document", ctx)
    filename = f"bill-{ctx['number']}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/{bill_id}/send-email")
async def post_bill_send_email(
    bill_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Render the bill PDF and send via customer_email (kill-switch gated)."""
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.customer_email import (
        CommsServiceError,
        CustomerEmailAttachment,
        CustomerEmailError,
        send_customer_email,
    )
    from saebooks.services.latex_pdf import render_latex

    tenant_id = resolve_tenant_id(request)
    payload = await request.json()
    try:
        from_addr = str(payload["from_addr"]).strip()
        to = [x.strip() for x in (payload.get("to") or []) if x and str(x).strip()]
        cc = [x.strip() for x in (payload.get("cc") or []) if x and str(x).strip()]
        bcc = [x.strip() for x in (payload.get("bcc") or []) if x and str(x).strip()]
        subject = str(payload["subject"]).strip()
        body_html = str(payload["body_html"]).strip()
    except (KeyError, TypeError) as exc:
        raise HTTPException(422, f"missing field: {exc}") from exc

    sent_by_uid_raw = payload.get("sent_by_user_id")
    sent_by_user_id: UUID | None = None
    if sent_by_uid_raw:
        try:
            sent_by_user_id = UUID(str(sent_by_uid_raw))
        except (ValueError, TypeError):
            sent_by_user_id = None

    if not from_addr or not to or not subject or not body_html:
        raise HTTPException(422, "from_addr, to, subject, and body_html are all required")

    bill = await svc.api_get(session, bill_id, tenant_id=tenant_id, company_id=company_id)
    if bill is None:
        raise HTTPException(404, "Bill not found")

    supplier = (
        await session.execute(sa_select(Contact).where(Contact.id == bill.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == bill.company_id))
    ).scalars().first()

    ctx = _build_bill_ctx(bill, supplier, company)
    ctx.setdefault("kind", "Bill")
    pdf_bytes = await render_latex("document", ctx)
    pdf_filename = f"bill-{ctx['number']}.pdf"

    try:
        result = await send_customer_email(
            session,
            tenant_id=tenant_id,
            doc_type="bill",
            doc_id=bill.id,
            doc_version=bill.version,
            sent_by_user_id=sent_by_user_id,
            from_addr=from_addr,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body_html=body_html,
            attachments=[CustomerEmailAttachment(
                filename=pdf_filename, content=pdf_bytes, content_type="application/pdf",
            )],
        )
    except CustomerEmailError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CommsServiceError as exc:
        raise HTTPException(502, f"comms service unavailable: {exc}") from exc

    await session.commit()

    return JSONResponse({
        "mode":        result.mode,
        "log_id":      str(result.log_id),
        "message_id":  result.message_id,
        "reason":      result.reason,
        "outbox_path": result.outbox_path,
    }, status_code=200)
