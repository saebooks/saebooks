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
    InvoiceLineAppendOut,
    InvoiceLineCreate,
    InvoiceLineOut,
    InvoiceListOut,
    InvoiceOut,
    InvoiceRecoveryBody,
    InvoiceUpdate,
    InvoiceWriteOffBody,
    ReviewFlagBody,
)
from saebooks.config import settings
from saebooks.models.invoice import InvoiceStatus
from saebooks.services import bad_debt as bad_debt_svc
from saebooks.services import invoices as svc
from saebooks.services import review_flags as review_flags_svc
from saebooks.services.authz import no_additional_gate, require_permission_or_role
from saebooks.services.features import (
    FLAG_SMTP_RELAY,
    FLAG_STRIPE_INTEGRATION,
    feature_enabled_for_request,
    require_feature,
)
from saebooks.services.fx import gate_non_base_currency
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
    await gate_non_base_currency(session, request, company_id, payload.currency)
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
            source_quote_id=payload.source_quote_id,
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
# Append a single line (POST /{id}/lines — DRAFT-only, returns the new line)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/lines",
    status_code=201,
    responses={
        201: {"model": InvoiceLineAppendOut},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
)
async def append_invoice_line(
    invoice_id: UUID,
    payload: InvoiceLineCreate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Append one line to a DRAFT invoice, returning the created line (incl id).

    DRAFT-only, optimistic-locked (``If-Match: <version>``), version-bumped
    and change_log'd like every other edit. Preserves the invoice's existing
    lines — unlike PATCH, which replaces them wholesale. Built for the
    time-entries → invoice-line hand-off, which needs the new line's id to
    write back into ``time_entries.invoice_line_id`` without clobbering the
    ids of lines earlier converted onto the same DRAFT.
    """
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with invoice version is required")

    tenant_id = resolve_tenant_id(request)
    # Confirm existence + tenant/company ownership up front so a missing or
    # foreign invoice is a clean 404; any InvoiceError below is therefore a
    # validation failure (bad line FK / not-DRAFT) → 422.
    if await svc.api_get(
        session, invoice_id, tenant_id=tenant_id, company_id=company_id
    ) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        inv, line = await svc.api_append_line(
            session,
            invoice_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            line=payload.model_dump(),
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        body = InvoiceConflictBody(
            detail="version mismatch",
            current=InvoiceOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except (ValueError, svc.InvoiceError) as exc:
        raise HTTPException(422, str(exc)) from exc

    out = InvoiceLineAppendOut(
        invoice_id=inv.id,
        invoice_version=inv.version,
        line=InvoiceLineOut.model_validate(line),
    )
    return JSONResponse(json.loads(out.model_dump_json()), status_code=201)


# ---------------------------------------------------------------------------
# Void / soft-delete (DELETE with If-Match → 204)
# ---------------------------------------------------------------------------


@router.delete(
    "/{invoice_id}",
    responses={
        204: {"description": "Voided"},
        409: {"model": InvoiceConflictBody, "description": "Version mismatch"},
    },
    dependencies=[
        Depends(require_permission_or_role("invoice.void", no_additional_gate))
    ],
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
    dependencies=[
        Depends(require_permission_or_role("invoice.post", no_additional_gate))
    ],
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
    dependencies=[
        Depends(require_permission_or_role("invoice.void", no_additional_gate))
    ],
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
# Bad-debt write-off (POST /{id}/write-off → WRITTEN_OFF)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/write-off",
    responses={200: {"model": InvoiceOut}},
    dependencies=[
        Depends(require_permission_or_role("invoice.write_off", no_additional_gate))
    ],
)
async def write_off_invoice_endpoint(
    invoice_id: UUID,
    request: Request,
    body: InvoiceWriteOffBody | None = None,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Write off a POSTED invoice's unpaid balance as a bad debt.

    Reclaims GST on taxable lines (decreasing adjustment), settles the invoice
    (WRITTEN_OFF), and removes it from aged receivables. 409 if the invoice is
    already written off / has nothing unpaid; 404 if not found; 422 otherwise.
    """
    tenant_id = resolve_tenant_id(request)
    payload = body or InvoiceWriteOffBody()
    write_off_date = payload.write_off_date or date.today()

    if await svc.api_get(
        session, invoice_id, tenant_id=tenant_id, company_id=company_id
    ) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        inv = await bad_debt_svc.write_off_invoice(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            write_off_date=write_off_date,
            posted_by=f"api:{bearer[:8]}…",
            reason=payload.reason,
        )
    except bad_debt_svc.BadDebtError as exc:
        msg = str(exc)
        low = msg.lower()
        if "not found" in low:
            raise HTTPException(404, msg) from exc
        if "already written off" in low or "nothing to write off" in low:
            raise HTTPException(409, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(_dump(inv), status_code=200)


# ---------------------------------------------------------------------------
# Bad-debt recovery (POST /{id}/record-recovery → Other Income, no GST)
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/record-recovery",
    status_code=201,
    dependencies=[
        Depends(require_permission_or_role("invoice.recovery", no_additional_gate))
    ],
)
async def record_recovery_endpoint(
    invoice_id: UUID,
    body: InvoiceRecoveryBody,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Record money received against a written-off debt as Other Income (no GST).

    Posts Dr <bank> / Cr 4-1290 Bad Debt Recovery. 409 if the invoice is not
    written off; 404 if invoice / bank account not found; 422 otherwise.
    """
    tenant_id = resolve_tenant_id(request)
    recovery_date = body.recovery_date or date.today()

    if await svc.api_get(
        session, invoice_id, tenant_id=tenant_id, company_id=company_id
    ) is None:
        raise HTTPException(404, "Invoice not found")

    try:
        entry = await bad_debt_svc.record_recovery(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            bank_account_id=body.bank_account_id,
            amount=body.amount,
            recovery_date=recovery_date,
            posted_by=f"api:{bearer[:8]}…",
            payer_contact_id=body.payer_contact_id,
        )
    except bad_debt_svc.BadDebtError as exc:
        msg = str(exc)
        low = msg.lower()
        if "not found" in low:
            raise HTTPException(404, msg) from exc
        if "not written_off" in low or "written-off" in low or "written off" in low:
            raise HTTPException(409, msg) from exc
        raise HTTPException(422, msg) from exc

    return JSONResponse(
        {
            "journal_entry_id": str(entry.id),
            "invoice_id": str(invoice_id),
            "amount": str(body.amount),
            "recovery_date": recovery_date.isoformat(),
            "bank_account_id": str(body.bank_account_id),
        },
        status_code=201,
    )


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


def _build_invoice_ctx(
    inv: Any, customer: Any, company: Any, bank_account: Any = None
) -> dict[str, Any]:
    """Construct the render-document ctx from an invoice + its customer + company.

    ``bank_account`` is the company's account flagged ``show_on_invoice``
    (see bank_accounts_svc.get_remittance_account); when present its ABA
    fields drive the How-to-Pay panel, falling back to the company's
    static ``bank_*`` columns (0168).
    """
    from saebooks.services.bank_accounts import remit_bank_details

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
    bank_details = remit_bank_details(company, bank_account)
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
        # Remit-to bank details (0171): flagged account first, company
        # bank_* columns as fallback. company.bank keeps the shipped 0168
        # template contract; bank_details is the same dict at top level.
        "bank_details": bank_details,
        "company": {
            "name":    (company.legal_name or company.name) if company else "",
            "abn":     (company.abn or "") if company else "",
            "phone":   (company.phone or "") if company else "",
            "email":   (company.email or "") if company else "",
            "website": (company.website or "") if company else "",
            "address": company_addr,  # supplier block uses the company's own address
            **({k: v for k, v in company_addr.items()} if company_addr else {}),
            "bank": bank_details,
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


@router.get("/{invoice_id}/render-context")
async def get_invoice_render_context(
    invoice_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Return the fact context the app render service needs to build the PDF.

    Engine = accountant (facts); app = bookkeeper (presentation). This is the
    exact ``_build_invoice_ctx`` dict — including the show_on_invoice bank
    resolution — that the /pdf route feeds to the render service, exposed so
    the app can own rendering. ``kind`` is returned alongside ``ctx`` (the
    /pdf route injects it into ctx before rendering).
    """
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.models.contact import Contact
    from saebooks.services.bank_accounts import get_remittance_account

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
    bank_account = await get_remittance_account(session, inv.company_id)

    ctx = _build_invoice_ctx(inv, customer, company, bank_account)
    return JSONResponse({"template": "document", "kind": "Tax Invoice", "ctx": ctx})


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
    from saebooks.services.bank_accounts import get_remittance_account
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
    bank_account = await get_remittance_account(session, inv.company_id)

    ctx = _build_invoice_ctx(inv, customer, company, bank_account)
    ctx.setdefault("kind", "Tax Invoice")
    pdf_bytes = await render_latex("document", ctx)
    filename = f"invoice-{ctx['number']}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/{invoice_id}/einvoice.xml")
async def get_invoice_einvoice_xml(
    invoice_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Generate the EN 16931 / Peppol BIS Billing 3.0 UBL Invoice XML for a
    POSTED, EUR-denominated invoice. Always regenerated; never stored.

    Seller identity: the generator resolves the seller's primary registry
    number (BT-30) from the company's business identifiers; this route takes
    the seller country (BT-40) from ``Company.jurisdiction`` and resolves the
    seller VAT number (BT-31) from the company's ``<juris>_vat`` business
    identifier (e.g. ``ee_vat``) — the same identifier registry the regcode
    comes from. BT-31 is mandatory for a Standard-rated line under EN 16931
    (BR-S-02); if the company has not recorded a VAT identifier, the
    generator refuses (422) rather than emit a standard-rate e-invoice with
    no VAT id on it. The engine has no structured street-address column, so
    street/city/postal are omitted — EN 16931 permits a country-only address.

    Errors mirror the generator's own refusals rather than 500-ing:
    * invoice not found / RLS-invisible / foreign tenant → 404
    * DRAFT, non-EUR, missing statutory identity (e.g. no resolvable seller
      registrikood, seller VAT id, or buyer country) → 422 with the
      generator's message.
    """
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.services import business_identifiers as biz_ids
    from saebooks.services.einvoice.generator import (
        EInvoiceError,
        SellerIdentity,
        generate_einvoice,
    )

    tenant_id = resolve_tenant_id(request)
    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")

    company = (
        await session.execute(sa_select(Company).where(Company.id == inv.company_id))
    ).scalars().first()
    # Company.jurisdiction is the 2-letter ISO-ish routing key (e.g. "EE");
    # use it for the seller country when well-formed, else fall back to the
    # SellerIdentity default. Anything not 2 alpha chars is not a valid
    # BT-40 country code.
    juris = (company.jurisdiction or "").strip() if company is not None else ""
    country_ok = len(juris) == 2 and juris.isalpha()

    # Seller VAT number (BT-31) from the jurisdiction's ``<juris>_vat``
    # business identifier (ee_vat, uk_vat, …). None when unrecorded — the
    # generator then refuses a Standard-rated line (BR-S-02) with a 422.
    seller_vat: str | None = None
    if company is not None and country_ok:
        vat_scheme = f"{juris.lower()}_vat"
        if vat_scheme in biz_ids.KNOWN_SCHEMES:
            row = await biz_ids.get(session, company.id, vat_scheme)
            seller_vat = row.value if row is not None else None

    seller = (
        SellerIdentity(country_code=juris.upper(), vat_number=seller_vat)
        if country_ok
        else SellerIdentity(vat_number=seller_vat)
    )

    try:
        xml_bytes = await generate_einvoice(session, invoice_id, seller=seller)
    except EInvoiceError as exc:
        raise HTTPException(422, str(exc)) from exc

    number = inv.number or str(inv.id)[:8]
    filename = f"invoice-{number}.xml"
    return Response(
        content=xml_bytes,
        media_type="application/xml",
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

    inv = await svc.api_get(session, invoice_id, tenant_id=tenant_id, company_id=company_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")

    customer = (
        await session.execute(sa_select(Contact).where(Contact.id == inv.contact_id))
    ).scalars().first()
    company = (
        await session.execute(sa_select(Company).where(Company.id == inv.company_id))
    ).scalars().first()
    from saebooks.services.bank_accounts import get_remittance_account
    bank_account = await get_remittance_account(session, inv.company_id)

    ctx = _build_invoice_ctx(inv, customer, company, bank_account)
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
            sae_relay_entitled=feature_enabled_for_request(FLAG_SMTP_RELAY, request),
        )
    except CustomerEmailError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CommsServiceError as exc:
        raise HTTPException(502, f"comms service unavailable: {exc}") from exc

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
