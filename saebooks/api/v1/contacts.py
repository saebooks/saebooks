"""Pure JSON contacts router — ``/api/v1/contacts``.

Implements the Phase 0 scaffolding pattern that Phase 1 will apply to
every other entity:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN`` or JWT.
* Optimistic locking via ``If-Match: <version>`` header on update/delete.
* Retry-safe writes via ``X-Idempotency-Key: <uuid>`` header — replayed
  requests return the cached response body + status without re-executing.
* Every write appends a row to ``change_log`` (handled by the service
  layer, not the router).

P0 cross-tenant leak fix
------------------------
This router is the leak's epicentre and the first to migrate to the
shared ``get_session`` dep — see ``saebooks.api.v1.deps``. Behaviour
changes:

* The active company is resolved by the shared ``get_active_company_id``
  dep — callers may pin a specific company via ``X-Company-Id``;
  otherwise the first active company for the tenant is used.
* ``get_contact`` now passes the request tenant to ``svc.get`` so a
  detail lookup for a foreign-tenant UUID returns 404 even if the
  caller knows the UUID.
* Every handler accepts a single ``Depends(get_session)`` session
  with ``app.current_tenant`` set, so the FORCE-RLS policy gates
  every query in the handler.

Idempotency migration (audit-trail #10)
----------------------------------------
Replaced the race-unsafe ``_idempotent_replay`` / ``_remember_idempotent``
helpers (against legacy ``idempotency_keys`` table) with the race-safe
``claim_or_fetch`` / ``store_response`` service (``idempotency_records``
table).  SHA-256 of the raw request body is passed so conflicting bodies
on the same key return HTTP 422 per RFC 8417 §2.1.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    ConflictBody,
    ContactCreate,
    ContactListOut,
    ContactOut,
    ContactUpdate,
)
from saebooks.models.contact import Contact, ContactType
from saebooks.services import contacts as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/contacts",
    tags=["contacts"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_if_match(header: str | None) -> int | None:
    """Parse the ``If-Match`` header as a version integer."""
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


def _dump(contact: Contact) -> dict[str, Any]:
    """Pydantic-serialise a Contact row."""
    return json.loads(ContactOut.model_validate(contact).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=ContactListOut)
async def list_contacts(
    contact_type: ContactType | None = Query(default=None, alias="type"),
    search: str | None = Query(default=None, alias="q"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ContactListOut:
    # Count (matches filter minus limit/offset).
    count_stmt = (
        select(func.count())
        .select_from(Contact)
        .where(Contact.company_id == company_id, Contact.archived_at.is_(None))
    )
    if contact_type is not None:
        count_stmt = count_stmt.where(Contact.contact_type == contact_type)
    if search:
        pattern = f"%{search}%"
        count_stmt = count_stmt.where(
            Contact.name.ilike(pattern) | Contact.email.ilike(pattern)
        )
    total = (await session.execute(count_stmt)).scalar_one()
    items = await svc.list_active(
        session,
        company_id,
        contact_type=contact_type,
        search=search,
        limit=limit,
        offset=offset,
    )
    return ContactListOut(
        items=[ContactOut.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class _BulkTagOneOffIn(BaseModel):
    contact_ids: list[UUID]
    is_one_off: bool


@router.post("/bulk-tag-one-off")
async def bulk_tag_one_off(
    payload: _BulkTagOneOffIn,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> dict[str, int]:
    """Flip ``is_one_off`` on the given contacts. Returns ``{"flipped": N}``
    counting only contacts whose flag actually changed (idempotent). Defined
    before /{contact_id} so the literal path wins the route match."""
    tenant_id = resolve_tenant_id(request)
    flipped = 0
    for cid in payload.contact_ids:
        contact = await svc.get(session, cid, tenant_id=tenant_id)
        if contact is None or contact.archived_at is not None:
            continue
        if contact.is_one_off == payload.is_one_off:
            continue
        await svc.update(
            session, cid, actor="api", tenant_id=tenant_id,
            is_one_off=payload.is_one_off,
        )
        flipped += 1
    return {"flipped": flipped}


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> ContactOut:
    tenant_id = resolve_tenant_id(request)
    contact = await svc.get(session, contact_id, tenant_id=tenant_id, company_id=company_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    return ContactOut.model_validate(contact)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ContactOut,
    status_code=201,
)
async def create_contact(
    payload: ContactCreate,
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
        # CLAIMED — fall through to write

    try:
        contact = await svc.create(
            session,
            company_id,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
            **payload.model_dump(exclude_unset=False, exclude={"bank_bsb", "bank_account_number", "bank_account_title"}),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # svc.create already committed; refresh inside the session before
    # we dump to dodge a MissingGreenlet when pydantic walks the row.
    await session.refresh(contact)
    body = _dump(contact)
    if key is not None:
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{contact_id}",
    responses={
        200: {"model": ContactOut},
        409: {"model": ConflictBody, "description": "Version mismatch"},
    },
)
async def update_contact(
    contact_id: UUID,
    payload: ContactUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with contact version is required")
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
                status_code=claim.response_status or 200,
            )

    # Belt-and-braces: check the contact exists and is owned by this
    # tenant before we attempt the update. RLS already enforces this,
    # but the service-layer ValueError message is friendlier than a
    # silent zero-rows update.
    if await svc.get(session, contact_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Contact not found")

    try:
        contact = await svc.update(
            session,
            contact_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = ConflictBody(
            detail="version mismatch",
            current=ContactOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(contact)
    body = _dump(contact)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft — archive)
# ---------------------------------------------------------------------------


@router.delete(
    "/{contact_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": ConflictBody, "description": "Version mismatch"},
    },
)
async def archive_contact(
    contact_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    if hard:
        existing = await svc.get(session, contact_id, tenant_id=tenant_id, company_id=company_id)
        if existing is None:
            raise HTTPException(404, "Contact not found")
        await hard_delete_with_audit(
            session, existing, "contacts", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with contact version is required")
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

    if await svc.get(session, contact_id, tenant_id=tenant_id, company_id=company_id) is None:
        raise HTTPException(404, "Contact not found")

    try:
        contact = await svc.archive(
            session,
            contact_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            tenant_id=tenant_id,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = ConflictBody(
            detail="version mismatch",
            current=ContactOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    if key is not None:
        archived_body = json.dumps({"archived": str(contact.id)}).encode()
        await store_response(session, key, 204, archived_body)
        await session.commit()
    return Response(status_code=204)

# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Customer statements (G3) — added 2026-05-27.
# JSON + PDF surfaces for "this customer's AR activity in a date range".
# Built on services.statements.build_statement.
# ---------------------------------------------------------------------------


@router.get("/{contact_id}/statement")
async def get_contact_statement(
    contact_id: UUID,
    request: Request,
    period_from: date = Query(..., alias="from", description="Statement period start (YYYY-MM-DD)"),
    period_to:   date = Query(..., alias="to",   description="Statement period end (YYYY-MM-DD)"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> JSONResponse:
    """Return a customer statement as JSON for the given period."""
    from saebooks.services.statements import build_statement

    tenant_id = resolve_tenant_id(request)
    if period_to < period_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    try:
        statement = await build_statement(
            session, contact_id,
            tenant_id=tenant_id, company_id=company_id,
            period_start=period_from, period_end=period_to,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    return JSONResponse({
        "contact_id":    str(statement.contact_id),
        "contact_name":  statement.contact_name,
        "contact_email": statement.contact_email,
        "period": {
            "from": statement.period_start.isoformat(),
            "to":   statement.period_end.isoformat(),
        },
        "opening_balance":           str(statement.opening_balance),
        "closing_balance":           str(statement.closing_balance),
        "total_invoiced_in_period":  str(statement.total_invoiced_in_period),
        "total_paid_in_period":      str(statement.total_paid_in_period),
        "lines": [
            {
                "date":        ln.line_date.isoformat(),
                "kind":        ln.kind,
                "reference":   ln.reference,
                "description": ln.description,
                "amount_dr":   str(ln.amount_dr),
                "amount_cr":   str(ln.amount_cr),
                "balance":     str(ln.balance),
            }
            for ln in statement.lines
        ],
    })


@router.get("/{contact_id}/statement.pdf")
async def get_contact_statement_pdf(
    contact_id: UUID,
    request: Request,
    period_from: date = Query(..., alias="from"),
    period_to:   date = Query(..., alias="to"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Response:
    """Return the customer statement as a PDF (engineering-style document)."""
    from sqlalchemy import select as sa_select

    from saebooks.models.company import Company
    from saebooks.services.statements import build_statement, render_statement_pdf

    tenant_id = resolve_tenant_id(request)
    if period_to < period_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    try:
        statement = await build_statement(
            session, contact_id,
            tenant_id=tenant_id, company_id=company_id,
            period_start=period_from, period_end=period_to,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    company = (
        await session.execute(sa_select(Company).where(Company.id == company_id))
    ).scalars().first()

    pdf_bytes = render_statement_pdf(statement, company=company)
    filename = f"statement-{statement.contact_name.replace(' ', '-')[:40]}-{period_from.isoformat()}-{period_to.isoformat()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
