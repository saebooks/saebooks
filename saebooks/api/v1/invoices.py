"""JSON router — ``/api/v1/invoices``.

Phase 1 tier-3 accounts-receivable endpoint.

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* ``DELETE`` is a soft-void (archived_at + VOIDED) returning 204.
* Lines are nested in the response.
* B/48: ``POST /{id}/stripe-payment-link`` generates a Stripe Checkout
  Session URL (gated on ``FLAG_STRIPE_INTEGRATION``).

P0 cross-tenant leak fix
------------------------
All handlers now share a single ``Depends(get_session)`` session per
request. ``app.current_tenant`` is bound at the connection level by
``get_session``; all queries within the request are gated by the
``tenant_isolation`` RLS policy from migration 0055. The active
company is resolved by the shared ``get_active_company_id`` dep —
callers may pin a specific company via ``X-Company-Id``; otherwise
the first active company for the tenant is used.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_active_user_id, get_session
from saebooks.api.v1.edit_force_gate import edit_force_admin_gate
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    InvoiceConflictBody,
    InvoiceCreate,
    InvoiceListOut,
    InvoiceOut,
    InvoiceUpdate,
    ReviewFlagBody,
)
from saebooks.config import settings
from saebooks.models.invoice import InvoiceStatus
from saebooks.services import invoices as svc
from saebooks.services import review_flags as review_flags_svc
from saebooks.services.features import FLAG_STRIPE_INTEGRATION, require_feature
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response
from saebooks.services.integrations import StripeError, StripeNotConfiguredError
from saebooks.services.integrations.stripe_links import create_payment_link

router = APIRouter(
    prefix="/invoices",
    tags=["invoices"],
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


def _dump(inv: Any) -> dict[str, Any]:
    return json.loads(InvoiceOut.model_validate(inv).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=InvoiceListOut)
async def list_invoices(
    request: Request,
    contact_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    flagged: bool | None = Query(default=None, description="Filter by flagged_for_review"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> InvoiceListOut:
    offset = (page - 1) * page_size
    status_enum: InvoiceStatus | None = None
    if status is not None:
        try:
            status_enum = InvoiceStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(400, f"Invalid status '{status}'") from exc

    tenant_id = resolve_tenant_id(request)
    invoices, total = await svc.list_active(
        session,
        company_id,
        tenant_id,
        contact_id=contact_id,
        status=status_enum,
        flagged=flagged,
        date_from=date_from,
        date_to=date_to,
        limit=page_size,
        offset=offset,
    )
    return InvoiceListOut(
        items=[InvoiceOut.model_validate(inv) for inv in invoices],
        total=total,
        limit=page_size,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> InvoiceOut:
    tenant_id = resolve_tenant_id(request)
    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return InvoiceOut.model_validate(inv)


# ---------------------------------------------------------------------------
# POST /{id}/review-flag — Gap 3 (set/clear flag for review)
# ---------------------------------------------------------------------------


@router.post("/{invoice_id}/review-flag", response_model=InvoiceOut)
async def set_invoice_review_flag(
    invoice_id: UUID,
    payload: ReviewFlagBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> InvoiceOut:
    tenant_id = resolve_tenant_id(request)
    actor = getattr(request.state, "actor", None) or "api"
    try:
        await review_flags_svc.set_review_flag(
            session,
            "invoice",
            invoice_id,
            tenant_id=tenant_id,
            company_id=company_id,
            actor=str(actor),
            flagged=payload.flagged,
            review_note=payload.review_note,
        )
    except review_flags_svc.ReviewFlagError as exc:
        raise HTTPException(404, str(exc)) from exc
    inv = await svc.api_get(
        session, invoice_id, tenant_id=tenant_id, company_id=company_id
    )
    return InvoiceOut.model_validate(inv)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    payload: InvoiceCreate,
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
        inv = await svc.api_create(
            session,
            company_id,
            tenant_id,
            actor=f"api:{bearer[:8]}…",
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            due_date=payload.due_date,
            lines=[line.model_dump() for line in payload.lines],
            notes=payload.notes,
            payment_terms=payload.payment_terms,
            currency=payload.currency,
            settlement_date=payload.settlement_date,
        )
    except (ValueError, svc.InvoiceError) as exc:
        raise HTTPException(422, str(exc)) from exc

    body = _dump(inv)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{invoice_id}",
    responses={
        200: {"model": InvoiceOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def update_invoice(
    invoice_id: UUID,
    payload: InvoiceUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    force: bool = Depends(edit_force_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    tenant_id = resolve_tenant_id(request)
    # Confirm the invoice belongs to the caller's tenant before touching it.
    if await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        lines_data = (
            [line.model_dump() for line in payload.lines]
            if payload.lines is not None
            else None
        )
        inv = await svc.api_update(
            session,
            invoice_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            force=force,
            contact_id=payload.contact_id,
            issue_date=payload.issue_date,
            due_date=payload.due_date,
            notes=payload.notes,
            payment_terms=payload.payment_terms,
            lines=lines_data,
            settlement_date=payload.settlement_date,
        )
    except svc.VersionConflict as exc:
        body = InvoiceConflictBody(
            detail="version mismatch",
            current=InvoiceOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.InvoiceError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(inv)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{invoice_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def void_invoice(
    invoice_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if existing is None:
        raise HTTPException(404, "Invoice not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "invoices", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    try:
        if existing.status == svc.InvoiceStatus.DRAFT:
            # DELETE soft-deletes: a DRAFT archives (no JE reversal, op="archive").
            # POST /{id}/void stays strict (api_void_invoice rejects DRAFT 422).
            await svc.api_void(
                session,
                invoice_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
            )
        else:
            await svc.api_void_invoice(
                session,
                invoice_id,
                actor=f"api:{bearer[:8]}…",
                expected_version=expected,
                tenant_id=tenant_id,
                actor_user_id=await get_active_user_id(request),
            )
    except svc.VersionConflict as exc:
        body = InvoiceConflictBody(
            detail="version mismatch",
            current=InvoiceOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.InvoiceError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Post / status transition (POST /{id}/post → POSTED)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/post",
    responses={
        200: {"model": InvoiceOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def post_invoice(
    invoice_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition invoice DRAFT → POSTED, generating journal entry lines."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

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

    if await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        inv = await svc.api_post_invoice(
            session,
            invoice_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        body = InvoiceConflictBody(
            detail="version mismatch",
            current=InvoiceOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.InvoiceError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(inv)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Void via status transition (POST /{id}/void → VOIDED with JE reversal)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/void",
    responses={
        200: {"model": InvoiceOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def void_invoice_transition(
    invoice_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Transition any non-VOIDED invoice → VOIDED, reversing JE if POSTED."""
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

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

    if await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        inv = await svc.api_void_invoice(
            session,
            invoice_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            actor_user_id=await get_active_user_id(request),
        )
    except svc.VersionConflict as exc:
        body = InvoiceConflictBody(
            detail="version mismatch",
            current=InvoiceOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.InvoiceError) as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    body = _dump(inv)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Stripe payment link (B/48 — POST /{id}/stripe-payment-link)
# Gated on FLAG_STRIPE_INTEGRATION (Business+).
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/stripe-payment-link",
    dependencies=[Depends(require_feature(FLAG_STRIPE_INTEGRATION))],
)
async def create_stripe_payment_link(
    invoice_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Generate a Stripe Checkout Session URL for a posted invoice."""
    tenant_id = resolve_tenant_id(request)
    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")

    if inv.status != InvoiceStatus.POSTED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Invoice must be POSTED to generate a payment link "
            f"(current status: {inv.status.value})",
        )

    balance_due = inv.total - inv.amount_paid
    if balance_due <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Invoice has no outstanding balance; payment link not generated",
        )

    invoice_dict: dict[str, Any] = {
        "id": str(inv.id),
        "number": inv.number,
        "currency": inv.currency,
        "total": inv.total,
        "lines": [
            {
                "description": line.description,
                "line_total": line.line_total,
            }
            for line in inv.lines
        ],
    }

    try:
        url = await create_payment_link(invoice_dict, settings=settings)
    except StripeNotConfiguredError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Stripe is not configured: {exc}",
        ) from exc
    except StripeError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Stripe API error: {exc}",
        ) from exc

    inv.stripe_payment_link = url
    await session.commit()

    return JSONResponse({"payment_link": url})


# ---------------------------------------------------------------------------
# PDF + send-email — added 2026-05-26 (parity with quotes /pdf and
# /send-email). Both endpoints render via render_invoice_pdf; /send-email
# pushes through the customer_email kill-switch (two-key gate: env flag
# + per-tenant outbound_email_enabled). NEVER bypasses the gate.
# ---------------------------------------------------------------------------


def _build_invoice_ctx(inv: Any, customer: Any, company: Any) -> dict[str, Any]:
    """Construct the render-document ctx from an invoice + its customer + company."""
    customer_addr = {}
    if customer:
        customer_addr = {k: v for k, v in {
            "address_line1": customer.address_line1,
            "address_line2": customer.address_line2,
            "city":          customer.city,
            "state":         customer.state,
            "postcode":      customer.postcode,
            "country":       customer.country,
        }.items() if v}
    company_addr = (company.address or {}) if company else {}
    return {
        "number":       inv.number or str(inv.id)[:8],
        "issue_date":   inv.issue_date.isoformat() if inv.issue_date else "",
        "due_date":     inv.due_date.isoformat() if inv.due_date else "",
        "currency":     inv.currency,
        "subtotal":     str(inv.subtotal),
        "tax_total":    str(inv.tax_total),
        "total":        str(inv.total),
        "amount_paid":  str(inv.amount_paid),
        "notes":        inv.notes or "",
        "payment_terms": inv.payment_terms or "",
        "payment_terms_text": (company.payment_terms_text or "") if company else "",
        "terms_url":          (company.terms_url or "") if company else "",
        "company": {
            "name":    (company.legal_name or company.name) if company else "",
            "abn":     (company.abn or "") if company else "",
            "address": company_addr,  # supplier block uses the company's own address
            **({k: v for k, v in company_addr.items()} if company_addr else {}),
            "bank": {
                "name":           (company.bank_name or "") if company else "",
                "bsb":            (company.bank_bsb or "") if company else "",
                "account_number": (company.bank_account_number or "") if company else "",
                "account_name":   (company.bank_account_name or "") if company else "",
            },
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
            for ln in inv.lines
        ],
    }


@router.get("/{invoice_id}/pdf")
async def get_invoice_pdf(
    invoice_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Render an invoice as PDF (Tax Invoice layout). Always regenerated; never stored."""
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.latex_pdf import render_latex

    tenant_id = resolve_tenant_id(request)
    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")

    customer = (
        await session.execute(sa_select(Contact).where(Contact.id == inv.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == inv.company_id))
    ).scalars().first()

    ctx = _build_invoice_ctx(inv, customer, company)
    ctx.setdefault("kind", "Tax Invoice")
    pdf_bytes = await render_latex("document", ctx)
    filename = f"invoice-{ctx['number']}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/{invoice_id}/send-email")
async def post_invoice_send_email(
    invoice_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Render the invoice PDF and send (or block) via customer_email.

    Request JSON:
        {
            "from_addr": "billing@yourbiz.com.au",
            "to":   ["customer@example.com"],
            "cc":   [],                              // optional
            "bcc":  [],                              // optional
            "subject": "Tax Invoice 1234 from ...",
            "body_html": "<p>Please find attached…</p>"
        }
    Response: { mode, log_id, message_id?, reason?, outbox_path? }
    """
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.customer_email import (
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

    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")

    customer = (
        await session.execute(sa_select(Contact).where(Contact.id == inv.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == inv.company_id))
    ).scalars().first()

    ctx = _build_invoice_ctx(inv, customer, company)
    ctx.setdefault("kind", "Tax Invoice")
    pdf_bytes = await render_latex("document", ctx)
    pdf_filename = f"invoice-{ctx['number']}.pdf"

    try:
        result = await send_customer_email(
            session,
            tenant_id=tenant_id,
            doc_type="invoice",
            doc_id=inv.id,
            doc_version=inv.version,
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

    # Stamp sent_at on POSTED invoices (drafts can be re-rendered; sent_at
    # is only meaningful once the document is final).
    if inv.status == InvoiceStatus.POSTED and inv.sent_at is None:
        from datetime import datetime
        inv.sent_at = datetime.now(UTC)
    await session.commit()

    return JSONResponse({
        "mode":        result.mode,
        "log_id":      str(result.log_id),
        "message_id":  result.message_id,
        "reason":      result.reason,
        "outbox_path": result.outbox_path,
    }, status_code=200)
